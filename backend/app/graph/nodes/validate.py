"""validate_plan: server-side validation of a submit_plan call against this run's ledger.

Valid -> finalize (complete, agent). Invalid with attempts left -> the violations go back to the model as the
submit_plan ToolMessage (status="error"), keeping the history append-only (a repair). Attempts exhausted or
the deadline close -> deterministic fallback.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from app.domain.fill import cap_final_stop
from app.domain.quality import improve_underfilled
from app.domain.schemas import ValidationReport
from app.domain.validator import draft_from_payload, validate_plan
from app.graph.nodes.common import (
    AGENT_NO_TIME,
    AGENT_REPAIRS_EXHAUSTED,
    DEADLINE_RESERVE_S,
    fill_draft,
    tracer_of,
)
from app.graph.state import PlannerState, RunContext, dump, get_constraints, get_ctx, get_ledger, get_prefs
from app.graph.tools import (
    SUBMIT_PLAN,
    dumps,
    fix_line,
    normalize_plan_payload,
    schedule_view,
    violation_dicts,
    with_turns_left,
)

REPAIR_INSTRUCTION = ("Fix every error and call submit_plan again. Use only candidate IDs returned by your tools, "
                      "keep durations inside each candidate's visit range, and keep numbers out of the prose.")


async def validate_submission(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    settings = rc.deps.settings
    pending = state.get("pending_submit") or {}
    attempts = int(state.get("submit_attempts") or 0) + 1
    max_attempts = settings.max_submit_attempts
    kinds: Counter[str] = Counter(state.get("protocol_error_kinds") or {})

    ledger = get_ledger(state)
    ctx = get_ctx(state)
    trimmed_minutes = 0
    with tracer.node("validate_plan") as info:
        draft, schema_errors = draft_from_payload(normalize_plan_payload(pending.get("args")))
        if draft is None:
            kinds["schema_invalid"] += 1
            report = ValidationReport(ok=False, violations=schema_errors)
        else:
            proposed_minutes = sum(s.duration_min for s in draft.stops)
            draft = cap_final_stop(draft, ledger, ctx, get_prefs(state),
                                   get_constraints(state, "original"), rc.deps.travel)
            trimmed_minutes = proposed_minutes - sum(s.duration_min for s in draft.stops)
            report = validate_plan(draft, ledger, ctx, get_prefs(state), get_constraints(state, "original"),
                                   rc.deps.travel)
        codes = sorted({v.code for v in report.errors})
        if "UNKNOWN_ID" in codes:
            kinds["unknown_id"] += 1
        filled_minutes = 0
        quality_improved = False
        quality_error: str | None = None
        if report.ok and draft is not None:  # stretch toward the window; only a still-valid result is used
            draft, filled_minutes = fill_draft(draft, ledger, ctx, get_prefs(state), get_constraints(state, "enforced"),
                                               get_constraints(state, "original"), rc.deps.travel)
            if rc.remaining() > DEADLINE_RESERVE_S + 1:
                try:
                    improved = await asyncio.wait_for(asyncio.to_thread(
                        improve_underfilled, draft, ledger, ctx, get_prefs(state),
                        get_constraints(state, "original"), rc.deps.travel), 0.75)
                except Exception as exc:  # optional quality search; cancellation still propagates
                    quality_error = type(exc).__name__
                    improved = draft
                if improved is not draft:
                    draft, filled_minutes = fill_draft(improved, ledger, ctx, get_prefs(state),
                        get_constraints(state, "enforced"), get_constraints(state, "original"), rc.deps.travel)
                    quality_improved = True
            report = validate_plan(draft, ledger, ctx, get_prefs(state), get_constraints(state, "original"), rc.deps.travel)
        info["summary"] = (f"attempt {attempts}/{max_attempts}: "
                           + ("valid" if report.ok else f"{len(report.errors)} error(s): {', '.join(codes)}")
                           + ("; improved an underfilled proposal" if quality_improved else "")
                           + (f"; visits lengthened by {filled_minutes} min to use the window" if filled_minutes
                              else ""))
        info["data"] = {"attempt": attempts, "ok": report.ok, "codes": codes, "filled_minutes": filled_minutes,
                        "quality_improved": quality_improved, "quality_error": quality_error, "trimmed_minutes": trimmed_minutes}
    tracer.emit("validation", node="validate_plan", summary="passed" if report.ok else "failed: " + ", ".join(codes),
                data={"ok": report.ok, "attempt": attempts, "max_attempts": max_attempts, "source": "agent",
                      "filled_minutes": filled_minutes, "quality_improved": quality_improved,
                      "trimmed_minutes": trimmed_minutes, "violations": violation_dicts(report.violations, include_ids=False)})

    first_valid = state.get("first_submit_valid")
    update: dict[str, Any] = {"submit_attempts": attempts, "pending_submit": None,
                              "protocol_errors": sum(kinds.values()), "protocol_error_kinds": dict(kinds),
                              "first_submit_valid": report.ok if attempts == 1 else first_valid}
    if trimmed_minutes:
        update["extra_assumptions"] = [f"The final venue visit was shortened by {trimmed_minutes} min to finish within your window."]
    if quality_improved:
        note = "The system replaced a short AI proposal with a fuller validated plan from the places it searched."
        update["extra_assumptions"] = [*update.get("extra_assumptions", []), note]
    call_id = str(pending.get("id") or "submit_plan")
    if report.ok and draft is not None:
        tracer.emit("tool_completed", node="validate_plan", tool=SUBMIT_PLAN, summary="plan accepted",
                    data={"attempt": attempts})
        update["messages"] = [ToolMessage(content=dumps({"ok": True, "message": "Plan accepted."}),
                                          tool_call_id=call_id, name=SUBMIT_PLAN)]
        update["final"] = {"status": "complete", "mode": "agent", "draft": dump(draft)}
        update["filled_minutes"] = filled_minutes
        update["route"] = "finalize"
        return update

    can_repair = attempts < max_attempts and rc.remaining() >= DEADLINE_RESERVE_S
    tracer.emit("tool_failed", node="validate_plan", tool=SUBMIT_PLAN, summary="rejected: " + ", ".join(codes),
                data={"code": "VALIDATION_FAILED", "codes": codes, "attempt": attempts, "recovered": can_repair})
    body: dict[str, Any] = {
        "ok": False, "attempt": attempts, "attempts_left": max_attempts - attempts if can_repair else 0,
        "fix": fix_line(report.errors, draft, ledger=ledger, constraints=get_constraints(state, "enforced"),
                        ctx=ctx, plan=report.plan),
        "errors": violation_dicts(report.errors), "warnings": violation_dicts(report.warnings),
        **schedule_view(report.plan),  # the computed times, so a repair doesn't need a check_plan call
    }
    if can_repair:
        body["instruction"] = REPAIR_INSTRUCTION
        update["route"] = "plan_agent"
        update["degradations"] = []
    else:
        body["instruction"] = "No submissions left; the system will build the plan."
        # reported by fallback_planner (which may still use a draft that passed check_plan when time ran out)
        update["agent_stop"] = AGENT_REPAIRS_EXHAUSTED.format(n=attempts) if attempts >= max_attempts else AGENT_NO_TIME
        update["route"] = "fallback_planner"
    turns_left = int(state.get("turn_cap") or settings.max_agent_turns) - int(state.get("agent_turns") or 0)
    update["messages"] = [ToolMessage(content=with_turns_left(dumps(body), turns_left), tool_call_id=call_id,
                                      name=SUBMIT_PLAN, status="error")]
    return update
