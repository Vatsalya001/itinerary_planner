"""Authoritative plan validation (see IMPLEMENTATION_PLAN.md section 7). OWNER: domain agent. Contract only."""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.schemas import (
    Candidate, CheckItem, Constraint, PlanDraft, Preferences, ValidationReport, WorldContext,
)
from app.providers.base import TravelEstimator

# code -> one-line meaning. Severity is decided per check; WEATHER_RISK and CONSTRAINT_DROPPED are warnings.
CODES: dict[str, str] = {
    "SCHEMA_INVALID": "Draft failed schema validation",
    "UNKNOWN_ID": "Candidate ID not in this run's ledger",
    "CITY_MISMATCH": "Candidate more than 40 km from the resolved city centre",
    "DUPLICATE_STOP": "Same candidate used twice",
    "DURATION_OUT_OF_RANGE": "Duration outside the candidate's allowed range",
    "BUDGET_EXCEEDED": "Worst-case cost above budget",
    "TIME_EXCEEDED": "Elapsed time above the available window",
    "HOURS_CONFLICT": "Visit overlaps a known closed period",
    "HOURS_UNVERIFIED": "Opening hours unknown (warning)",
    "DIET_UNVERIFIED": "Strict diet without positive evidence",
    "DIET_CONFLICT": "Contrary diet evidence",
    "ACCESSIBILITY_UNVERIFIED": "Wheelchair constraint without positive evidence",
    "ALCOHOL_CONFLICT": "Alcohol venue with alcohol:avoid",
    "SETTING_CONFLICT": "Indoor/outdoor setting constraint violated",
    "MUST_INCLUDE_MISSING": "A must_include requirement is not met",
    "MUST_AVOID_VIOLATED": "A must_avoid category is present",
    "CROWD_LIKELY": "Avoid-crowds constraint and a likely-busy stop at its scheduled time",
    "PACING_EXCEEDED": "Stops, walking or long legs above the energy profile",
    "DAYLIGHT_CONFLICT": "Outdoor walk scheduled after sunset",
    "WEATHER_RISK": "Outdoor stop with rain >= 50% and no Plan B (warning)",
    "EVENT_DATE_MISMATCH": "Event not on the target date",
    "SOURCE_MISSING": "Stop without a provider origin",
    "TRANSFER_UNKNOWN": "Leg could not be estimated (missing coordinates)",
    "PROSE_NUMERIC_CLAIM": "Currency amounts, durations or clock times in model prose",
    "CONSTRAINT_DROPPED": "An original hard constraint was missing from parsed preferences (warning; still enforced)",
    "PLAN_B_INVALID": "Plan B candidate unknown, outdoor, or itself violating constraints",
}


def validate_plan(draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext, prefs: Preferences,
                  original_constraints: list[Constraint], travel: TravelEstimator) -> ValidationReport:
    """Recompute everything from ledger records; enforce merge(prefs.hard_constraints, original_constraints).
    Never trusts model-provided numbers. Pure function."""
    raise NotImplementedError


def summarize_checks(report: ValidationReport, ctx: WorldContext, prefs: Preferences,
                     constraints: list[Constraint]) -> list[CheckItem]:
    """Checks panel: budget, time, opening_hours, diet, accessibility, crowds, weather, sources."""
    raise NotImplementedError
