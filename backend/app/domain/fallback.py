"""Deterministic planner (no LLM): prefilter, score, beam search, validate. OWNER: domain agent. Contract only."""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.schemas import Candidate, Constraint, PlanDraft, Preferences, WorldContext
from app.providers.base import TravelEstimator


def plan_deterministic(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                       constraints: list[Constraint], travel: TravelEstimator) -> PlanDraft | None:
    """Best valid draft or None. Every returned draft passes validate_plan with no errors.
    why/tradeoff texts are templates with no numbers (PROSE_NUMERIC_CLAIM must not fire)."""
    raise NotImplementedError
