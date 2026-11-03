"""fallback_planner and explain_infeasible: the deterministic path (also the whole planner for the baseline).

The deterministic planner runs over the ledger first (what the agent already found). If that yields nothing,
the default searches run through the shared pipeline (snapshot fallback included) and it tries again over
the union. No plan -> explain_infeasible (or an honest "place data unavailable" when nothing could be
searched). Every plan is re-validated with the same checks as an agent plan.

Before any of that, when the agent loop stopped for time (timeout, deadline, turn cap) or ended text-only after
its nudge, and its latest check_plan draft had no errors, that draft is re-validated and used (status
complete, mode agent, with a note saying so). The reason
the agent stopped is reported here, once, with wording that matches what actually happened.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.runtime import Runtime

from app.domain.fallback import plan_deterministic
from app.domain.infeasible import explain_infeasible
from app.domain.parser_rules import default_queries
from app.domain.schemas import PlanDraft, ValidationReport
from app.domain.validator import validate_plan
from app.graph.nodes.common import AGENT_NO_SUBMIT, SALVAGE_NOTES, degrade, fill_draft, tracer_of
from app.graph.state import PlannerState, RunContext, dump, get_constraints, get_ctx, get_ledger, get_prefs
from app.graph.tools import violation_dicts
from app.llm.registry import is_baseline
from app.pipeline import PLACES_UNAVAILABLE, search_events, search_places


def _checked_draft_report(state: PlannerState, rc: RunContext) -> tuple[PlanDraft, ValidationReport] | None:
    """The agent's latest check_plan draft (only kept when that check had no errors), re-validated now, when
    the loop stopped for time, at the turn cap or with a text-only reply after its nudge (D-P2-10)."""
    raw = state.get("checked_draft")
    if not raw or state.get("agent_stop") not in SALVAGE_NOTES:
        return None
    draft = PlanDraft.model_validate(raw)
    report = validate_plan(draft, get_ledger(state), get_ctx(state), get_prefs(state),
                           get_constraints(state, "original"), rc.deps.travel)
    return draft, report


async def fallback_planner(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    deps = rc.deps
    prefs = get_prefs(state)
    ctx = get_ctx(state)
    enforced = get_constraints(state, "enforced")
    original = get_constraints(state, "original")
    ledger = get_ledger(state)
    stop = state.get("agent_stop")
    new_ledger: dict[str, Any] = {}
    degradations: list[str] = []
    baseline = is_baseline(state.get("model_id") or rc.model_id)

    with tracer.node("fallback_planner") as info:
        salvaged = _checked_draft_report(state, rc)
        filled_minutes = 0
        if salvaged is not None and salvaged[1].ok:
            used, filled_minutes = fill_draft(salvaged[0], ledger, ctx, prefs, enforced, original, deps.travel)
            salvaged = (used, salvaged[1])
        if salvaged is not None:
            tracer.emit("validation", node="fallback_planner", summary="passed" if salvaged[1].ok else "failed",
                        data={"ok": salvaged[1].ok, "attempt": 1, "source": "agent_checked_draft",
                              "filled_minutes": filled_minutes,
                              "violations": violation_dicts(salvaged[1].violations, include_ids=False)})
        if salvaged is not None and salvaged[1].ok:
            info["summary"] = f"used the agent's checked draft ({len(salvaged[0].stops)} stop(s))"
            info["data"] = {"ledger_size": len(ledger), "source": "agent_checked_draft",
                            "filled_minutes": filled_minutes}
            note = SALVAGE_NOTES[str(stop)]
            return {"degradations": degrade(tracer, "fallback_planner", note, stop=stop),
                    "final": {"status": "complete", "mode": "agent", "draft": dump(salvaged[0])},
                    "filled_minutes": filled_minutes, "route": "finalize"}

        draft = plan_deterministic(list(ledger.values()), ctx, prefs, enforced, deps.travel) if ledger else None
        searched = False
        search_ok = True
        if draft is None:
            searched = True
            if state.get("places_mode") in {"snapshot", "unavailable"} and deps.snapshot is not None and deps.snapshot.covers(ctx.location):
                provider = deps.snapshot
                batches = await asyncio.gather(*(provider.search(q, ctx) for q in default_queries(prefs, ctx)))
                found, place_deg, ok = [c for b in batches for c in b], ["Using the curated snapshot"], True
            elif state.get("places_mode") in {"snapshot", "unavailable"}:
                found, place_deg, ok = [], [PLACES_UNAVAILABLE], False
            else:
                try:
                    found, place_deg, ok = await asyncio.wait_for(
                        search_places(default_queries(prefs, ctx), ctx, deps, tracer), max(0.05, rc.remaining() - 3))
                except TimeoutError:
                    found, place_deg, ok = [], [PLACES_UNAVAILABLE], False
            events: list[Any] = []
            event_deg: list[str] = []
            if not state.get("events_failed") and rc.remaining() > 2:
                try:
                    events, event_deg = await asyncio.wait_for(
                        search_events(prefs, ctx, deps, tracer), max(0.05, rc.remaining() - 2))
                except TimeoutError:
                    event_deg = ["Event search exceeded the remaining time; using available places"]
            degradations += place_deg + event_deg
            search_ok = ok
            for c in [*found, *events]:
                if c.id not in ledger:
                    ledger[c.id] = c
                    new_ledger[c.id] = dump(c)
            draft = plan_deterministic(list(ledger.values()), ctx, prefs, enforced, deps.travel) if ledger else None
        report = validate_plan(draft, ledger, ctx, prefs, original, deps.travel) if draft is not None else None
        if report is not None:
            tracer.emit("validation", node="fallback_planner", summary="passed" if report.ok else "failed",
                        data={"ok": report.ok, "attempt": 1, "source": "deterministic",
                              "violations": violation_dicts(report.violations, include_ids=False)})
        planned = draft is not None and report is not None and report.ok
        # Why the agent stopped is a degradation unless the model said "nothing fits" and the deterministic
        # search confirms it: then the model was right (D-P2-09).
        if stop and (planned or stop != AGENT_NO_SUBMIT):
            degradations += degrade(tracer, "fallback_planner", stop)
        info["summary"] = (f"{len(draft.stops)} stop(s) over {len(ledger)} candidate(s)" if draft is not None
                           else f"no feasible combination over {len(ledger)} candidate(s)")
        info["data"] = {"ledger_size": len(ledger), "searched_defaults": searched}

    update: dict[str, Any] = {"ledger": new_ledger, "degradations": degradations}
    if planned and draft is not None:
        update["final"] = {"status": "complete" if baseline else "fallback", "mode": "deterministic",
                           "draft": dump(draft)}
        update["route"] = "finalize"
    elif not search_ok and not any(c.kind == "place" for c in ledger.values()):
        update["final"] = {"status": "no_feasible_plan", "mode": "deterministic",
                           "message": f"{PLACES_UNAVAILABLE}. Please try again in a few minutes."}
        update["route"] = "finalize"
    else:
        update["route"] = "explain_infeasible"
    return update


async def explain_infeasible_node(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    with tracer.node("explain_infeasible") as info:
        report = explain_infeasible(list(get_ledger(state).values()), get_ctx(state), get_prefs(state),
                                    get_constraints(state, "enforced"), rc.deps.travel)
        info["summary"] = report.reason
        info["data"] = {"binding_constraints": report.binding_constraints,
                        "relaxations": [r.label for r in report.relaxations]}
    return {"final": {"status": "no_feasible_plan", "mode": "deterministic", "infeasible": dump(report),
                      "message": report.reason},
            "route": "finalize"}
