"""tools: executes the last AIMessage's tool calls (our own executor, not the prebuilt ToolNode).

For every call: unknown names and invalid arguments become ToolMessage(status="error") with a short fix
hint and count as protocol errors; per-tool caps are enforced; every returned candidate goes into the
ledger; tool_called / tool_completed / tool_failed trace events carry the model's ``reason``. A
``submit_plan`` call is not executed here: it is handed to validate_plan, which answers it.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from pydantic import ValidationError

from app.graph.nodes.common import AGENT_NO_TIME, DEADLINE_RESERVE_S, degrade, tracer_of
from app.graph.state import (
    PlannerState,
    RunContext,
    dump,
    get_constraints,
    get_ctx,
    get_ledger,
    get_prefs,
)
from app.graph.tools import (
    ARG_MODELS,
    CHECK_PLAN,
    QUERIES_KEY,
    SEARCH_EVENTS,
    SEARCH_PLACES,
    SUBMIT_PLAN,
    TOOL_CAPS,
    TOOL_NAMES,
    EstimateTravelArgs,
    SearchEventsArgs,
    SearchPlacesArgs,
    ToolEnv,
    ToolOutcome,
    clip_reason,
    dumps,
    error_outcome,
    run_check_plan,
    run_estimate_travel,
    run_search_events,
    run_search_places,
    validation_errors_text,
    with_turns_left,
)
from app.pipeline import PLACES_UNAVAILABLE
from app.trace import Tracer

log = logging.getLogger("app.graph")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_]")


def safe_tool_name(name: Any) -> str:
    return _SAFE_NAME.sub("", str(name or ""))[:40] or "unknown"


def _last_ai(state: PlannerState) -> AIMessage | None:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage):
            return message
    return None


def _emit_outcome(tracer: Tracer, name: str, outcome: ToolOutcome, duration_ms: int) -> None:
    data = dict(outcome.data or {})
    if outcome.status == "error" or outcome.provider_failed:
        data.setdefault("code", "TOOL_ERROR")
        data["recovered"] = outcome.recovered
        tracer.emit("tool_failed", node="tools", tool=name, duration_ms=duration_ms, summary=outcome.summary,
                    data=data)
    else:
        tracer.emit("tool_completed", node="tools", tool=name, duration_ms=duration_ms, summary=outcome.summary,
                    data=data or None)


async def _execute(name: str, args: Any, env: ToolEnv) -> ToolOutcome:
    if name == CHECK_PLAN:
        return run_check_plan(args, env)
    model = ARG_MODELS[name]
    try:
        parsed = model.model_validate(args if isinstance(args, dict) else {})
    except ValidationError as exc:
        return error_outcome(f"invalid arguments for {name}: {validation_errors_text(exc)}", protocol=True,
                             code="SCHEMA_INVALID")
    if name == SEARCH_PLACES:
        assert isinstance(parsed, SearchPlacesArgs)
        return await run_search_places(parsed, env)
    if name == SEARCH_EVENTS:
        assert isinstance(parsed, SearchEventsArgs)
        return await run_search_events(parsed, env)
    assert isinstance(parsed, EstimateTravelArgs)
    return run_estimate_travel(parsed, env)


async def run_tools(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    ai = _last_ai(state)
    env = ToolEnv(deps=rc.deps, ctx=get_ctx(state), prefs=get_prefs(state),
                  enforced=get_constraints(state, "enforced"), original=get_constraints(state, "original"),
                  ledger=get_ledger(state), places_mode=state.get("places_mode", "live"),  # type: ignore[arg-type]
                  events_failed=bool(state.get("events_failed")),
                  queries_used=int((state.get("tool_exec") or {}).get(QUERIES_KEY, 0)))
    requested: Counter[str] = Counter(state.get("tool_calls") or {})
    executed: Counter[str] = Counter(state.get("tool_exec") or {})
    kinds: Counter[str] = Counter(state.get("protocol_error_kinds") or {})
    checked_draft = state.get("checked_draft")
    known_degradations = set(state.get("degradations") or [])
    messages: list[ToolMessage] = []
    new_ledger: dict[str, Any] = {}
    degradations: list[str] = []
    pending: dict[str, Any] | None = None

    with tracer.node("tools") as info:
        for bad in (ai.invalid_tool_calls if ai else []):
            name = safe_tool_name(bad.get("name"))
            requested[name] += 1
            kinds["malformed_args"] += 1
            tracer.emit("tool_called", node="tools", tool=name)
            tracer.emit("tool_failed", node="tools", tool=name, duration_ms=0,
                        summary="arguments were not valid JSON", data={"code": "MALFORMED_ARGS", "recovered": True})
            messages.append(ToolMessage(
                content=dumps({"error": "Arguments were not valid JSON. Send one JSON object matching the schema."}),
                tool_call_id=bad.get("id") or "invalid_call", name=name, status="error"))

        for call in (ai.tool_calls if ai else []):
            name = str(call["name"])
            args = call.get("args")
            call_id = call.get("id") or f"call_{len(messages)}"
            reason = clip_reason(args.get("reason")) if isinstance(args, dict) else ""
            if name not in TOOL_NAMES:
                label = safe_tool_name(name)
                requested[label] += 1
                kinds["unknown_tool"] += 1
                tracer.emit("tool_called", node="tools", tool=label, reason=reason or None)
                tracer.emit("tool_failed", node="tools", tool=label, duration_ms=0, summary="unknown tool",
                            data={"code": "UNKNOWN_TOOL", "recovered": True})
                messages.append(ToolMessage(
                    content=dumps({"error": f"Unknown tool '{label}'. Available tools: {', '.join(TOOL_NAMES)}."}),
                    tool_call_id=call_id, name=label, status="error"))
                continue
            requested[name] += 1
            tracer.emit("tool_called", node="tools", tool=name, reason=reason or None)
            if name == SUBMIT_PLAN:
                if pending is None:
                    pending = {"id": call_id, "args": args if isinstance(args, dict) else {}, "reason": reason}
                else:
                    kinds["duplicate_submit"] += 1
                    tracer.emit("tool_failed", node="tools", tool=name, duration_ms=0,
                                summary="only one submit_plan per turn", data={"code": "DUPLICATE_SUBMIT",
                                                                              "recovered": True})
                    messages.append(ToolMessage(content=dumps({"error": "Only one submit_plan call per turn."}),
                                                tool_call_id=call_id, name=name, status="error"))
                continue
            if executed[name] >= TOOL_CAPS[name]:
                kinds["cap_reached"] += 1
                tracer.emit("tool_failed", node="tools", tool=name, duration_ms=0,
                            summary=f"limit of {TOOL_CAPS[name]} calls reached",
                            data={"code": "CAP_REACHED", "recovered": True})
                messages.append(ToolMessage(
                    content=dumps({"error": f"{name} can be called at most {TOOL_CAPS[name]} times per run. "
                                            "Work with the candidates you already have."}),
                    tool_call_id=call_id, name=name, status="error"))
                continue
            t0 = time.perf_counter()
            try:
                outcome = await _execute(name, args, env)
            except Exception as exc:  # a provider bug must not end the run; the model gets an error result
                log.warning("tool failed", extra={"tool": name, "error_type": type(exc).__name__})
                outcome = error_outcome(f"{name} failed unexpectedly; try a different query or continue without it",
                                        code="TOOL_ERROR")
            if outcome.executed:
                executed[name] += 1
            if outcome.protocol_kind:
                kinds[outcome.protocol_kind] += 1
            if name == CHECK_PLAN and outcome.executed:
                checked_draft = outcome.checked_draft  # the last check wins: None when it found errors
            _emit_outcome(tracer, name, outcome, int((time.perf_counter() - t0) * 1000))
            for message in outcome.degradations:
                if message not in known_degradations and message not in degradations:
                    degradations += degrade(tracer, "tools", message, tool=name)
            for c in outcome.candidates:
                if c.id not in env.ledger:
                    env.ledger[c.id] = c
                    new_ledger[c.id] = dump(c)
            messages.append(ToolMessage(content=outcome.content, tool_call_id=call_id, name=name,
                                        status=outcome.status, artifact=outcome.artifact))
        info["summary"] = (f"{len(messages) + (1 if pending else 0)} call(s); ledger {len(env.ledger)} candidate(s)"
                           + ("; plan submitted" if pending else ""))
        info["data"] = {"ledger_size": len(env.ledger), "places_mode": env.places_mode}

    executed[QUERIES_KEY] = env.queries_used
    turns_left = int(state.get("turn_cap") or rc.deps.settings.max_agent_turns) - int(state.get("agent_turns") or 0)
    messages = [m.model_copy(update={"content": with_turns_left(str(m.content), turns_left)}) for m in messages]
    has_places = any(c.kind == "place" for c in env.ledger.values())
    agent_stop: str | None = None
    if pending is not None:
        route = "validate_plan"
    elif env.places_mode == "unavailable" and not has_places:
        if PLACES_UNAVAILABLE not in known_degradations and PLACES_UNAVAILABLE not in degradations:
            degradations += degrade(tracer, "tools", PLACES_UNAVAILABLE)
        route = "fallback_planner"
    elif rc.remaining() < DEADLINE_RESERVE_S:
        agent_stop = AGENT_NO_TIME  # reported by fallback_planner, which may use checked_draft
        route = "fallback_planner"
    else:
        route = "plan_agent"
    return {
        "messages": messages, "ledger": new_ledger, "places_mode": env.places_mode,
        "events_failed": env.events_failed, "pending_submit": pending, "tool_calls": dict(requested),
        "tool_exec": dict(executed), "protocol_errors": sum(kinds.values()), "protocol_error_kinds": dict(kinds),
        "degradations": degradations, "checked_draft": checked_draft, "agent_stop": agent_stop, "route": route,
    }
