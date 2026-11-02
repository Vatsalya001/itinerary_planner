"""Hydrate a PlanDraft into a CanonicalPlan: clock times, legs, totals. OWNER: domain agent. Contract only."""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.schemas import CanonicalPlan, Candidate, PlanDraft, Preferences, Violation, WorldContext
from app.providers.base import TravelEstimator


def build_schedule(draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext, prefs: Preferences,
                   travel: TravelEstimator) -> tuple[CanonicalPlan | None, list[Violation]]:
    """start = ctx.start_min; each stop arrives at prev end + leg.minutes_max + pacing buffer.
    Events start at event_start_min (waiting is allowed and counts toward elapsed time).
    Returns (None, [UNKNOWN_ID/SCHEMA violations]) when hydration fails. Totals use ceil(10%) contingency.
    Also builds a Google Maps directions URL for the route."""
    raise NotImplementedError
