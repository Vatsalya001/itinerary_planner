"""No-LLM baseline planner: rules parser -> context -> candidate search -> deterministic planner -> validator.

This is the ``baseline:deterministic`` row in the model evals and the floor every model is compared with.
"""

from __future__ import annotations

import time

from app.deps import Deps
from app.domain.fallback import plan_deterministic
from app.domain.infeasible import explain_infeasible
from app.domain.parser_rules import (
    apply_defaults,
    clarification_questions,
    default_queries,
    merge_parse,
    parse_rules,
)
from app.domain.schemas import PlanRequest, PlanResult
from app.domain.validator import summarize_checks, validate_plan
from app.pipeline import (
    PLACES_UNAVAILABLE,
    build_context,
    enforced_constraints,
    geocode,
    search_events,
    search_places,
)
from app.providers.base import ProviderError
from app.providers.requirements_web import ResearchBudget, verify_candidates
from app.trace import Tracer, new_run_id


async def run_baseline(request: PlanRequest, deps: Deps, *, tracer: Tracer | None = None,
                       asked_before: bool = False) -> PlanResult:
    t0 = time.perf_counter()
    tracer = tracer or Tracer(new_run_id())
    run_id = tracer.run_id
    now = deps.now()
    tracer.emit("run_started", summary="Planning with the deterministic baseline (no LLM)",
                data={"mode": "deterministic"})

    def done(result: PlanResult) -> PlanResult:
        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        tracer.emit("result", data=result.model_dump(mode="json"))
        return result

    with tracer.node("parse_preferences") as info:
        outcome = merge_parse(None, parse_rules(request, now), request)
        info["summary"] = (
            f"city={outcome.preferences.city or '?'}, energy={outcome.preferences.energy}, "
            f"{len(outcome.preferences.hard_constraints)} hard constraint(s)"
        )

    matches = None
    if outcome.preferences.city:
        try:
            matches = await geocode(outcome.preferences.city, deps, tracer)
        except ProviderError:
            matches = None  # geocoder down: treat like unknown city only if nothing else blocks
    with tracer.node("check_clarity") as info:
        questions = clarification_questions(outcome, matches, asked_before)
        info["summary"] = f"{len(questions)} blocking question(s)"
    if questions:
        tracer.emit("clarification", node="ask_clarification",
                    data={"questions": [q.model_dump() for q in questions]})
        return done(PlanResult(run_id=run_id, status="needs_clarification", mode=None,
                               model="baseline:deterministic", questions=questions,
                               preferences=outcome.preferences))
    if not matches:
        return done(PlanResult(run_id=run_id, status="no_feasible_plan", mode="deterministic",
                               model="baseline:deterministic", preferences=outcome.preferences,
                               message=("I couldn't look up that city right now. Please try again shortly." if matches is None
                                        else f"No city called {outcome.preferences.city!r} was found.")))

    location = matches[0]
    prefs = apply_defaults(outcome, location)
    ctx, degradations = await build_context(prefs, location, deps, tracer, now)
    constraints, original = enforced_constraints(prefs, request)
    if prefs.available_minutes < 30:
        return done(PlanResult(run_id=run_id, status="no_feasible_plan", model="baseline:deterministic",
                               preferences=prefs, message="Your available window has ended or has too little time left."))

    if asked_before and outcome.unmapped_constraints:
        return done(PlanResult(run_id=run_id, status="no_feasible_plan", mode="deterministic",
            message="I cannot verify this mandatory requirement. Edit it or explicitly make it optional.",
            preferences=outcome.preferences))

    with tracer.node("gather_candidates") as info:
        candidates, place_deg, places_ok = await search_places(default_queries(prefs, ctx), ctx, deps, tracer)
        events, event_deg = await search_events(prefs, ctx, deps, tracer)
        info["summary"] = f"{len(candidates)} places, {len(events)} events"
    budget = ResearchBudget()
    candidates = await verify_candidates(candidates, prefs, ctx, deps, budget)
    events = await verify_candidates(events, prefs, ctx, deps, budget)
    degradations += place_deg + event_deg
    if not places_ok:
        return done(PlanResult(run_id=run_id, status="no_feasible_plan", mode="deterministic",
                               model="baseline:deterministic", preferences=prefs, degradations=degradations,
                               assumptions=prefs.assumptions,
                               message=f"{PLACES_UNAVAILABLE}. Please try again in a few minutes."))
    pool = candidates + events
    ledger = {c.id: c for c in pool}

    with tracer.node("fallback_planner") as info:
        draft = plan_deterministic(pool, ctx, prefs, constraints, deps.travel)
        info["summary"] = f"{len(draft.stops)} stop(s) proposed" if draft else "no feasible combination"

    if draft is None:
        with tracer.node("explain_infeasible") as info:
            report = explain_infeasible(pool, ctx, prefs, constraints, deps.travel)
            info["summary"] = report.reason
        return done(PlanResult(run_id=run_id, status="no_feasible_plan", mode="deterministic",
                               model="baseline:deterministic", preferences=prefs, infeasible=report,
                               degradations=degradations, assumptions=prefs.assumptions, message=report.reason))

    with tracer.node("validate_plan") as info:
        validation = validate_plan(draft, ledger, ctx, prefs, original, deps.travel)
        info["summary"] = "valid" if validation.ok else f"{len(validation.errors)} error(s)"
    tracer.emit("validation", node="validate_plan", summary="passed" if validation.ok else "failed",
                data={"ok": validation.ok, "violations": [v.model_dump() for v in validation.violations]})
    if not validation.ok or validation.plan is None:  # defensive: plan_deterministic only returns valid drafts
        report = explain_infeasible(pool, ctx, prefs, constraints, deps.travel)
        return done(PlanResult(run_id=run_id, status="no_feasible_plan", mode="deterministic",
                               model="baseline:deterministic", preferences=prefs, infeasible=report,
                               violations=validation.violations, degradations=degradations,
                               assumptions=prefs.assumptions, message=report.reason))

    return done(PlanResult(
        run_id=run_id,
        status="complete",
        mode="deterministic",
        model="baseline:deterministic",
        plan=validation.plan,
        checks=summarize_checks(validation, ctx, prefs, constraints),
        preferences=prefs,
        violations=validation.warnings,
        degradations=degradations,
        assumptions=prefs.assumptions,
        warnings=[v.message for v in validation.warnings],
    ))
