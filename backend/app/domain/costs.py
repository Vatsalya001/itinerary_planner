"""Per-person cost arithmetic for plans. OWNER: domain agent (Phase 1).

Rules (IMPLEMENTATION_PLAN.md section 7):

* A stop costs ``candidate.cost`` per person, independent of how long you stay.
* Legs cost ``leg.fare`` (estimates; per vehicle, counted in full per person as a conservative bound).
* contingency = ceil(10% of (stops_cost_max + legs_cost_max)); worst_case = upper total + contingency.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.schemas import Candidate, CostRange, Leg, Totals

CONTINGENCY_PERCENT = 10


def stop_cost(c: Candidate) -> CostRange:
    """Per-person cost of visiting ``c`` (duration does not change it)."""
    return c.cost


def contingency(upper_total: int) -> int:
    """ceil(10% of the upper total), in exact integer arithmetic."""
    return -(-upper_total * CONTINGENCY_PERCENT // 100)


def worst_case(stops_cost_max: int, legs_cost_max: int) -> int:
    upper = stops_cost_max + legs_cost_max
    return upper + contingency(upper)


def compute_totals(stops: Sequence[Candidate], legs: Sequence[Leg], *, currency: str, budget: int | None,
                   elapsed_min: int, available_minutes: int, walking_min: int) -> Totals:
    stops_min = sum(stop_cost(c).min for c in stops)
    stops_max = sum(stop_cost(c).max for c in stops)
    legs_min = sum(leg.fare.min for leg in legs)
    legs_max = sum(leg.fare.max for leg in legs)
    upper = stops_max + legs_max
    extra = contingency(upper)
    worst = upper + extra
    return Totals(
        currency=currency,
        stops_cost_min=stops_min,
        stops_cost_max=stops_max,
        legs_cost_min=legs_min,
        legs_cost_max=legs_max,
        contingency=extra,
        worst_case=worst,
        budget=budget,
        budget_left=None if budget is None else budget - worst,
        elapsed_min=elapsed_min,
        available_minutes=available_minutes,
        minutes_left=available_minutes - elapsed_min,
        walking_min=walking_min,
    )
