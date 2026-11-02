"""Explain why no plan fits and propose one-tap relaxations. OWNER: domain agent. Contract only."""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.schemas import Candidate, Constraint, InfeasibleReport, Preferences, WorldContext
from app.providers.base import TravelEstimator


def explain_infeasible(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                       constraints: list[Constraint], travel: TravelEstimator) -> InfeasibleReport:
    """Which constraint removed what; cheapest feasible budget and minimum minutes found by re-running the
    deterministic planner with budget/time relaxed; Relaxation patches use PlanRequest field names."""
    raise NotImplementedError
