"""Per-person cost arithmetic for plans. OWNER: domain agent (Phase 1).

Rules (IMPLEMENTATION_PLAN.md section 7):

* A stop costs ``candidate.cost`` per person, independent of how long you stay.
* Legs cost ``leg.fare`` (estimates; per vehicle, counted in full per person as a conservative bound).
* contingency = ceil(10% of (stops_cost_max + legs_cost_max)); worst_case = upper total + contingency.
* Amounts in different currencies are never summed: ``compute_totals`` raises ``CurrencyMismatchError``.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.schemas import Candidate, CostRange, Leg, Totals

CONTINGENCY_PERCENT = 10


class CurrencyMismatchError(ValueError):
    """A stop or leg is priced in a different currency from the plan's budget currency."""

    def __init__(self, currency: str, others: set[str]) -> None:
        super().__init__(f"costs in {sorted(others)} cannot be added to a {currency} total")
        self.currency = currency
        self.others = others


def foreign_currencies(currency: str, stops: Sequence[Candidate], legs: Sequence[Leg] = ()) -> set[str]:
    """Currencies other than ``currency`` used by these stops or legs."""
    found = {c.cost.currency for c in stops} | {leg.fare.currency for leg in legs}
    return {cur for cur in found if cur != currency}


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
                   elapsed_min: int, available_minutes: int, walking_min: int,
                   skip_foreign: bool = False) -> Totals:
    """Totals in ``currency``. Raises CurrencyMismatchError if anything is priced in another currency, unless
    ``skip_foreign`` (the caller has already raised CURRENCY_MISMATCH): then foreign amounts are left out, never
    added as if they were ``currency``."""
    others = foreign_currencies(currency, stops, legs)
    if others and not skip_foreign:
        raise CurrencyMismatchError(currency, others)
    stops = [c for c in stops if c.cost.currency == currency]
    legs = [leg for leg in legs if leg.fare.currency == currency]
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
