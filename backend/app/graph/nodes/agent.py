"""plan_agent: one model turn with the five tools bound (tool_choice left to the model).

Each turn appends the model's AIMessage unchanged (append-only history). Exceptions, timeouts, refusals,
the turn cap and the deadline route to the deterministic planner with a visible degradation. A text-only
turn gets one nudge; a second one goes to the fallback. Once a fallback model answers, the run sticks
with it for the remaining turns.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.graph.nodes.common import (
    AGENT_NO_SUBMIT,
    AGENT_NO_TIME,
    AGENT_NOT_CONFIGURED,
    AGENT_REFUSED,
    AGENT_TIMEOUT,
    AGENT_TURN_CAP,
    AGENT_UNAVAILABLE,
    DEADLINE_RESERVE_S,
    MIN_LLM_CALL_S,
    degrade,
    tracer_of,
)
from app.graph.prompts import AGENT_SYSTEM_PROMPT, NUDGE_TEXT, agent_payload, agent_payload_message
from app.graph.state import PlannerState, RunContext, get_constraints, get_ctx, get_prefs
from app.graph.tools import SUBMIT_PLAN, TOOL_CAPS, tool_schemas
from app.llm.registry import (
    METADATA_KEY,
    ModelRefusalError,
    RegistryError,
    build_agent_runnable,
    model_chain,
    turn_timeout_s,
)
from app.llm.usage import UsageTracker, empty_usage

log = logging.getLogger("app.graph")

PRIMARY_SHARE = 0.6  # share of a turn's budget the primary gets when a fallback model is configured


def _limits(state: PlannerState, rc: RunContext) -> dict[str, int]:
    settings = rc.deps.settings
    return {**TOOL_CAPS, SUBMIT_PLAN: settings.max_submit_attempts, "max_turns": settings.max_agent_turns}


def _summary(turn: int, ai: AIMessage) -> str:
    names = Counter(tc["name"] for tc in ai.tool_calls)
    parts = [f"{n}×{c}" if c > 1 else n for n, c in names.items()]
    if ai.invalid_tool_calls:
        parts.append(f"{len(ai.invalid_tool_calls)} malformed call(s)")
    return f"turn {turn}: " + (", ".join(parts) if parts else "text-only reply")


async def plan_agent(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    settings = rc.deps.settings
    requested = state.get("model_id") or rc.model_id
    full_chain = model_chain(settings, requested)
    used = state.get("model_used")
    chain = full_chain[full_chain.index(used):] if used in full_chain else full_chain
    history: list[AnyMessage] = list(state.get("messages") or [])
    new: list[AnyMessage] = []
    if not history:
        payload = agent_payload(get_prefs(state), get_ctx(state), get_constraints(state, "enforced"),
                                _limits(state, rc), events_available=rc.deps.events is not None)
        new.append(agent_payload_message(payload))
    turns = state.get("agent_turns", 0)
    usage: dict[str, Any] = empty_usage()
    degradations: list[str] = []
    failure: str | None = None
    ai: AIMessage | None = None

    with tracer.node("plan_agent") as info:
        timeout = min(turn_timeout_s(chain[0]), rc.remaining() - DEADLINE_RESERVE_S)
        if turns >= settings.max_agent_turns:
            failure = AGENT_TURN_CAP
        elif timeout < MIN_LLM_CALL_S:
            failure = AGENT_NO_TIME
        else:
            turns += 1
            tracker = UsageTracker()
            try:
                runnable = build_agent_runnable(
                    chain, tool_schemas(), settings,
                    primary_timeout_s=timeout * PRIMARY_SHARE if len(chain) > 1 else None,
                    primary_id=full_chain[0])
                result = await asyncio.wait_for(
                    runnable.ainvoke([SystemMessage(content=AGENT_SYSTEM_PROMPT), *history, *new],
                                     config={"callbacks": [tracker]}),
                    timeout)
                if not isinstance(result, AIMessage):
                    raise TypeError(f"expected AIMessage, got {type(result).__name__}")
                ai = result
            except TimeoutError:
                failure = AGENT_TIMEOUT
            except ModelRefusalError:
                failure = AGENT_REFUSED
            except RegistryError:
                failure = AGENT_NOT_CONFIGURED
            except Exception as exc:  # provider SDK errors vary; all of them mean "use the fallback planner"
                log.warning("agent turn failed", extra={"error_type": type(exc).__name__, "model": chain[0]})
                failure = AGENT_UNAVAILABLE
            usage = tracker.usage
        if ai is not None:
            info["summary"] = _summary(turns, ai)
            info["data"] = {"turn": turns, "model": ai.response_metadata.get(METADATA_KEY),
                            "tool_calls": [tc["name"] for tc in ai.tool_calls]}
        elif failure in (AGENT_TURN_CAP, AGENT_NO_TIME):
            info["summary"] = f"stopped before turn {turns + 1}: {failure}"
            info["data"] = {"turn": turns, "model": chain[0], "stop": failure}
        else:
            info["summary"] = f"turn {turns}: stopped ({failure})"
            info["data"] = {"turn": turns, "model": chain[0], "stop": failure}

    update: dict[str, Any] = {"agent_turns": turns, "usage": usage}
    if ai is None:
        # The degradation is decided by fallback_planner, which may still use a draft that passed check_plan.
        update["messages"] = new
        update["agent_stop"] = failure
        update["route"] = "fallback_planner"
        return update

    answered = str(ai.response_metadata.get(METADATA_KEY) or chain[0])
    if answered != full_chain[0] and used != answered:
        degradations += degrade(tracer, "plan_agent",
                                f"The primary AI model ({full_chain[0]}) failed; switched to {answered}",
                                from_model=full_chain[0], to_model=answered)
    update["model_used"] = answered
    new.append(ai)
    if ai.tool_calls or ai.invalid_tool_calls:
        update["route"] = "tools"
    elif state.get("nudges", 0) < 1:
        new.append(HumanMessage(content=NUDGE_TEXT))
        update["nudges"] = state.get("nudges", 0) + 1
        update["route"] = "plan_agent"
    else:  # a second text-only turn; if nothing is feasible, the model was right and this is no degradation
        update["agent_stop"] = AGENT_NO_SUBMIT
        update["route"] = "fallback_planner"
    update["messages"] = new
    update["degradations"] = degradations
    return update
