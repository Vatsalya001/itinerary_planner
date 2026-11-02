"""Constraint evidence, opening hours and crowd heuristics for a candidate at a scheduled time.

OWNER: domain agent (Phase 1). Contract only; implement every function below.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from app.domain.schemas import Candidate, Constraint, Evidence, WorldContext


class ConstraintCheck(NamedTuple):
    satisfied: bool  # True only with positive evidence (Evidence.CONFIRMED) or when not applicable
    evidence: Evidence
    code: str | None  # violation code when not satisfied, e.g. "DIET_UNVERIFIED"
    message: str


def opening_status(c: Candidate, start_min: int, end_min: int) -> Literal["open", "closed", "unknown"]:
    """"open" if [start, end) fits inside one known period; "closed" if hours are known and it doesn't;
    "unknown" if hours are unknown. Events are "open" exactly within [event_start_min, event_end_min]."""
    raise NotImplementedError


def likely_busy(c: Candidate, start_min: int, end_min: int) -> bool:
    """Heuristic: popularity == "high" and the visit overlaps any busy window."""
    raise NotImplementedError


def check_constraint(c: Candidate, constraint: Constraint, start_min: int, end_min: int,
                     ctx: WorldContext) -> ConstraintCheck:
    """Evaluate one hard constraint for a stop scheduled at [start, end). Unknown never satisfies."""
    raise NotImplementedError


def candidate_allowed(c: Candidate, constraints: list[Constraint], ctx: WorldContext) -> tuple[bool, list[str]]:
    """Time-independent prefilter: (allowed, reasons). Excludes known-violating candidates (e.g. non-veg
    food for diet:vegetarian, alcohol venues for alcohol:avoid, outdoor for indoors_only)."""
    raise NotImplementedError
