"""Improve severely underfilled AI drafts using only already-returned, validated candidates."""
from __future__ import annotations

import re
from collections.abc import Mapping

from app.domain.fallback import plan_deterministic
from app.domain.fill import DEFAULT_TARGET_RATIO
from app.domain.schemas import Candidate, Constraint, PlanDraft, Preferences, WorldContext
from app.domain.scoring import covered_interests
from app.domain.validator import UNDERFILL_MIN_WINDOW, validate_plan
from app.providers.base import TravelEstimator

_SINGLE_STOP = re.compile(r"\b(?:one|single)\s+(?:stop|activity|venue)\b|\b(?:only|just)\s+(?:a\s+)?caf[eé]\b", re.I)


def improve_underfilled(draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext,
                        prefs: Preferences, constraints: list[Constraint], travel: TravelEstimator) -> PlanDraft:
    """Never weaken hard constraints, lose covered interests or merely pad one stop.

    This is a bounded local search, not proof no better plan exists. The caller caps its optional runtime.
    A replacement is explicitly disclosed by the graph; the model still supplied the search and proposal.
    """
    if _SINGLE_STOP.search(" ".join([prefs.mood, *prefs.soft_preferences])):
        return draft
    base = validate_plan(draft, ledger, ctx, prefs, constraints, travel)
    if not base.ok or base.plan is None:
        return draft
    totals = base.plan.totals
    if totals.available_minutes < UNDERFILL_MIN_WINDOW or totals.elapsed_min >= DEFAULT_TARGET_RATIO * totals.available_minutes:
        return draft
    alternative = plan_deterministic(list(ledger.values()), ctx, prefs, constraints, travel)
    if alternative is None or len(alternative.stops) <= len(draft.stops):
        return draft
    checked = validate_plan(alternative, ledger, ctx, prefs, constraints, travel)
    if not checked.ok or checked.plan is None or checked.plan.totals.elapsed_min < totals.elapsed_min + 30:
        return draft
    def coverage(plan: PlanDraft) -> set[str]:
        return set().union(*(covered_interests(ledger[s.candidate_id], prefs.interests) for s in plan.stops))
    if not coverage(draft).issubset(coverage(alternative)):
        return draft
    return alternative
