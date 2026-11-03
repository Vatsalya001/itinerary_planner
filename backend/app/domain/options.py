"""A checked starting option for the model, built from the same bounded deterministic planner."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.domain.fallback import plan_deterministic
from app.domain.schemas import Candidate, Constraint, Preferences, WorldContext
from app.domain.scoring import covered_interests
from app.domain.validator import validate_plan
from app.providers.base import TravelEstimator


def validated_option(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                     constraints: list[Constraint], travel: TravelEstimator) -> dict[str, Any] | None:
    """Never promise feasibility without validation; no network or model calls.

    Keep the hint small: IDs/durations, computed bounds and interest coverage. Full candidate records
    remain in the run ledger. The model still chooses/submits; final validation remains mandatory.
    """
    ledger = {c.id: c for c in candidates}
    draft = plan_deterministic(list(ledger.values()), ctx, prefs, constraints, travel)
    if draft is None:
        return None
    report = validate_plan(draft, ledger, ctx, prefs, constraints, travel)
    if not report.ok or report.plan is None:
        return None
    covered: set[str] = set()
    stops: list[dict[str, Any]] = []
    for stop in draft.stops:
        covered.update(covered_interests(ledger[stop.candidate_id], prefs.interests))
        item: dict[str, Any] = {"candidate_id": stop.candidate_id, "duration_min": stop.duration_min,
                                "why": "Fits the checked itinerary."}
        if stop.plan_b_candidate_id:
            item["plan_b_candidate_id"] = stop.plan_b_candidate_id
        stops.append(item)
    return {
        "stops": stops,
        "summary_note": "Selected from available places and checked against your constraints.",
        "worst_case_cost": report.plan.totals.worst_case,
        "currency": report.plan.totals.currency,
        "elapsed_minutes": report.plan.totals.elapsed_min,
        "covered_interests": [i for i in prefs.interests if i in covered],
        "uncovered_interests": [i for i in prefs.interests if i not in covered],
    }
