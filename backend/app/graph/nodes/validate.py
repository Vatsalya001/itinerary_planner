"""validate_plan: server-side validation of a submit_plan call against this run's ledger.

Valid -> finalize (complete, agent). Invalid with attempts left -> the violations go back to the model as the
submit_plan ToolMessage (status="error"), keeping the history append-only (a repair). Attempts exhausted or
the deadline close -> deterministic fallback.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from app.domain.schemas import ValidationReport
from app.domain.validator import draft_from_payload, validate_plan
from app.graph.nodes.common import (
    AGENT_NO_TIME,
    AGENT_REPAIRS_EXHAUSTED,
    DEADLINE_RESERVE_S,
    tracer_of,
)
from app.graph.state import PlannerState, RunContext, dump, get_constraints, get_ctx, get_ledger, get_prefs
from app.graph.tools import (
    SUBMIT_PLAN,
    dumps,
    fix_line,
    normalize_plan_payload,
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

    with tracer.node("validate_plan") as info:
        draft, schema_errors = draft_from_payload(normalize_plan_payload(pending.get("args")))
        if draft is None:
            kinds["schema_invalid"] += 1
            report = ValidationReport(ok=False, violations=schema_errors)
        else:
            report = validate_plan(draft, get_ledger(state), get_ctx(state), get_prefs(state),
                                   get_constraints(state, "original"), rc.deps.travel)
        codes = sorted({v.code for v in report.errors})
        if "UNKNOWN_ID" in codes:
            kinds["unknown_id"] += 1
        info["summary"] = (f"attempt {attempts}/{max_attempts}: "
                           + ("valid" if report.ok else f"{len(report.errors)} error(s): {', '.join(codes)}"))
        info["data"] = {"attempt": attempts, "ok": report.ok, "codes": codes}
    tracer.emit("validation", node="validate_plan", summary="passed" if report.ok else "failed: " + ", ".join(codes),
                data={"ok": report.ok, "attempt": attempts, "max_attempts": max_attempts, "source": "agent",
                      "violations": violation_dicts(report.violations, include_ids=False)})

    first_valid = state.get("first_submit_valid")
    update: dict[str, Any] = {"submit_attempts": attempts, "pending_submit": None,
                              "protocol_errors": sum(kinds.values()), "protocol_error_kinds": dict(kinds),
                              "first_submit_valid": report.ok if attempts == 1 else first_valid}
    call_id = str(pending.get("id") or "submit_plan")
    if report.ok and draft is not None:
        tracer.emit("tool_completed", node="validate_plan", tool=SUBMIT_PLAN, summary="plan accepted",
                    data={"attempt": attempts})
        update["messages"] = [ToolMessage(content=dumps({"ok": True, "message": "Plan accepted."}),
                                          tool_call_id=call_id, name=SUBMIT_PLAN)]
        update["final"] = {"status": "complete", "mode": "agent", "draft": dump(draft)}
        update["route"] = "finalize"
        return update

    can_repair = attempts < max_attempts and rc.remaining() >= DEADLINE_RESERVE_S
    tracer.emit("tool_failed", node="validate_plan", tool=SUBMIT_PLAN, summary="rejected: " + ", ".join(codes),
                data={"code": "VALIDATION_FAILED", "codes": codes, "attempt": attempts, "recovered": can_repair})
    body: dict[str, Any] = {"ok": False, "attempt": attempts, "attempts_left": max_attempts - attempts if can_repair
                            else 0, "fix": fix_line(report.errors, draft), "errors": violation_dicts(report.errors),
                            "warnings": violation_dicts(report.warnings)}
    if can_repair:
        body["instruction"] = REPAIR_INSTRUCTION
        update["route"] = "plan_agent"
        update["degradations"] = []
    else:
        body["instruction"] = "No submissions left; the system will build the plan."
        # reported by fallback_planner (which may still use a draft that passed check_plan when time ran out)
        update["agent_stop"] = AGENT_REPAIRS_EXHAUSTED.format(n=attempts) if attempts >= max_attempts else AGENT_NO_TIME
        update["route"] = "fallback_planner"
    turns_left = settings.max_agent_turns - int(state.get("agent_turns") or 0)
    update["messages"] = [ToolMessage(content=with_turns_left(dumps(body), turns_left), tool_call_id=call_id,
                                      name=SUBMIT_PLAN, status="error")]
    return update
