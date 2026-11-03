"""finalize: builds the PlanResult, emits the "finalize" node_completed event with run metrics, then the single
"result" event. The plan shown is always recomputed here by the validator from ledger records; a draft
that somehow fails is never displayed. Checkpoint deletion happens in the runner after the graph ends.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from app.domain.explanations import ground_plan_text
from app.domain.infeasible import explain_infeasible
from app.domain.schemas import (
    Candidate,
    CanonicalPlan,
    CheckItem,
    InfeasibleReport,
    PlanDraft,
    PlanResult,
    Preferences,
    Violation,
)
from app.domain.validator import summarize_checks, validate_plan
from app.graph.nodes.common import free_time_warning, tracer_of
from app.graph.state import (
    PlannerState,
    RunContext,
    get_constraints,
    get_ctx,
    get_ledger,
    get_parse,
    get_prefs,
)
from app.llm.usage import add_usage


def usage_delta(total: dict[str, Any] | None, base: dict[str, Any] | None) -> dict[str, Any]:
    """``total - base`` per usage key (never negative): the usage of this call only."""
    out = add_usage(total, None)
    for key, value in (base or {}).items():
        if key in out:
            out[key] = max(0, out[key] - (value or 0))
    out["cost_usd"] = round(float(out["cost_usd"]), 6)
    return out


def run_metrics(state: PlannerState, model: str | None, rc: RunContext) -> dict[str, Any]:
    """Data for the "finalize" node_completed event (run log and eval metrics).

    * ``usage``: tokens, cost and LLM calls made by THIS call. A resumed call excludes the usage already
      reported by the call that paused for clarification, so every LLM call is counted exactly once.
    * ``tool_calls``: tool calls the model requested, by name (including rejected ones).
    * ``protocol_errors``: model tool calls that broke the tool protocol, counted once per call. Kinds (also in
      ``protocol_error_kinds``): ``malformed_args`` (arguments not valid JSON), ``unknown_tool``,
      ``schema_invalid`` (arguments fail the tool's schema, including a malformed submit_plan), ``unknown_id``
      (a call in any tool that references a candidate ID no tool returned this run: near_candidate_id,
      estimate_travel, check_plan or submit_plan stops), ``cap_reached`` (a call beyond the tool's per-run cap)
      and ``duplicate_submit`` (a second submit_plan in one turn). Validator findings other than UNKNOWN_ID
      (budget, hours, pacing...) are plan quality, not protocol errors.
    * ``filled_minutes``: minutes the code added to an accepted agent draft so it uses more of the window
      (app.domain.fill.fill_window; the filled draft passed validation again). 0 when nothing was added.
    * ``rate_limit_wait_ms``: time this call spent waiting for the client-side sliding-window rate limiter
      (app.llm.ratelimit) before model requests.
    * ``submit_attempts``, ``repairs`` (= attempts - 1), ``first_submit_valid`` (None without a submission),
      ``nudges``, ``agent_turns``; ``model`` is the model that answered last, ``requested_model`` the one asked for.
    """
    attempts = int(state.get("submit_attempts") or 0)
    kinds = {k: int(v) for k, v in (state.get("protocol_error_kinds") or {}).items() if v}
    usage = usage_delta(state.get("usage"), rc.usage_base)
    wait_ms = int(usage.pop("rate_limit_wait_ms", 0) or 0)
    return {
        "usage": usage,
        "rate_limit_wait_ms": wait_ms,
        "submit_attempts": attempts,
        "tool_calls": dict(state.get("tool_calls") or {}),
        "model": model,
        "requested_model": state.get("model_id") or rc.model_id,
        "filled_minutes": int(state.get("filled_minutes") or 0),
        "protocol_errors": sum(kinds.values()),
        "protocol_error_kinds": kinds,
        "repairs": max(0, attempts - 1),
        "first_submit_valid": state.get("first_submit_valid") if attempts else None,
        "nudges": int(state.get("nudges") or 0),
        "agent_turns": int(state.get("agent_turns") or 0),
    }


def with_omission_names(plan: CanonicalPlan, ledger: dict[str, Candidate]) -> CanonicalPlan:
    """Fill ``Omission.name`` from the ledger when the ref is a candidate ID (the UI hides raw IDs)."""
    omitted = [o.model_copy(update={"name": ledger[o.ref].name[:120]}) if o.ref in ledger and not o.name else o
               for o in plan.omitted]
    return plan.model_copy(update={"omitted": omitted})


def current_preferences(state: PlannerState) -> Preferences | None:
    if state.get("prefs"):
        return get_prefs(state)
    if state.get("parse"):
        return get_parse(state).preferences
    return None


async def finalize(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    final = dict(state.get("final") or {"status": "rejected", "message": "Nothing to finalize"})
    status = final.get("status", "rejected")
    mode = final.get("mode")
    prefs = current_preferences(state)
    model = "baseline:deterministic" if (state.get("model_id") or rc.model_id) == "baseline:deterministic" else (
        state.get("model_used") or state.get("model_id") or rc.model_id)

    with tracer.node("finalize") as info:
        plan: CanonicalPlan | None = None
        checks: list[CheckItem] = []
        violations: list[Violation] = []
        free_note: str | None = None
        infeasible = InfeasibleReport.model_validate(final["infeasible"]) if final.get("infeasible") else None
        message = final.get("message")
        if final.get("draft") and prefs is not None:
            ctx = get_ctx(state)
            enforced = get_constraints(state, "enforced")
            ledger = get_ledger(state)
            report = validate_plan(PlanDraft.model_validate(final["draft"]), ledger, ctx, prefs,
                                   get_constraints(state, "original"), rc.deps.travel)
            if report.ok and report.plan is not None:
                plan = with_omission_names(report.plan, ledger)
                if mode == "agent":
                    plan = ground_plan_text(plan, prefs, enforced, ctx)
                checks = summarize_checks(report, ctx, prefs, enforced)
                violations = report.warnings
                free_note = free_time_warning(plan, ctx)  # code-generated; never mixed into model prose
            else:  # defensive: never display a plan that fails validation
                infeasible = explain_infeasible(list(ledger.values()), ctx, prefs, enforced, rc.deps.travel)
                status, message = "no_feasible_plan", infeasible.reason
        assumptions = list(prefs.assumptions) if prefs is not None else []
        for extra in state.get("extra_assumptions") or []:
            if extra not in assumptions:
                assumptions.append(extra)
        result = PlanResult(
            run_id=rc.run_id, status=status, mode=mode, model=model, plan=plan, checks=checks, preferences=prefs,
            violations=violations, infeasible=infeasible, degradations=list(state.get("degradations") or []),
            assumptions=assumptions,
            warnings=[v.message for v in violations] + ([free_note] if free_note and plan is not None else []),
            message=message,
            latency_ms=rc.elapsed_ms(),
        )
        info["summary"] = f"{status}" + (f" ({mode})" if mode else "")
        info["data"] = run_metrics(state, model, rc)
    tracer.emit("result", data=result.model_dump(mode="json"))
    return {}
