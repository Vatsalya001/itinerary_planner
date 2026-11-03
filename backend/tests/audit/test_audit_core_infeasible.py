"""Independent audit (core): infeasibility explanations and one-tap relaxations.

A relaxation is a promise: "tap this and you get a plan". Each test applies the offered patch to the request the way
the client does (shallow merge) and re-runs the baseline. ``xfail(strict=True)`` = confirmed bug, see
docs/verification/AUDIT_CORE.md (AUD-<n>). Offline only.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.baseline import run_baseline
from app.deps import build_deps, fixture_settings
from app.domain.schemas import PlanRequest

# ---------------------------------------------------------------------------------------------------
# AUD-5 (P1): budget relaxation patches carry no currency; a structured foreign currency rescales them
# ---------------------------------------------------------------------------------------------------

async def test_aud5_raise_budget_patch_keeps_its_currency() -> None:
    deps = build_deps(fixture_settings())
    request: dict[str, Any] = {"city": "Bangalore", "budget": 1, "currency": "USD", "available_time": 240,
                               "start_time": "15:00", "interests": ["food", "music"], "constraints": ["vegetarian", "include a sit-down meal"]}
    first = await run_baseline(PlanRequest(**request), deps)
    assert first.status == "no_feasible_plan" and first.infeasible is not None
    raise_budget = next(r for r in first.infeasible.relaxations if r.label.startswith("Raise budget"))
    promised = raise_budget.patch["budget"]  # "Raise budget to ₹<promised>"
    again = await run_baseline(PlanRequest(**{**request, **raise_budget.patch}), deps, asked_before=True)
    assert again.preferences is not None
    assert (again.preferences.currency, again.preferences.budget) == ("INR", promised), (
        f"label promised ₹{promised}, the re-run planned with {again.preferences.currency} {again.preferences.budget}")


# ---------------------------------------------------------------------------------------------------
# AUD-6 (P1): no place data for the city, yet the report blames prices and offers "Raise budget"
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("settings", "request_data"), [
    # keyless default (PLACES_PROVIDER=auto without a Google key -> osm_snapshot): only Bangalore is covered
    ({"places_provider": "osm_snapshot"}, {"city": "Mumbai", "budget": 1000, "available_time": 240}),
    ({}, {"city": "London", "budget": 30, "currency": "GBP", "available_time": 180}),
])
async def test_aud6_empty_pool_relaxations_lead_somewhere(settings: dict[str, Any], request_data: dict[str, Any]) -> None:
    deps = build_deps(fixture_settings(**settings))
    first = await run_baseline(PlanRequest(**request_data), deps, asked_before=True)
    assert first.status == "no_feasible_plan" and first.infeasible is not None
    for rel in first.infeasible.relaxations:
        again = await run_baseline(PlanRequest(**{**request_data, **rel.patch}), deps, asked_before=True)
        assert again.status == "complete", f"'{rel.label}' leads to: {again.message}"
    assert "affordable" not in (first.message or ""), first.message


# ---------------------------------------------------------------------------------------------------
# AUD-7 (P1): relaxations are tested on the current pool, but applying one re-runs a different search
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("request_data", "weather"), [
    ({"city": "Mumbai", "budget": 500, "available_time": 600, "start_time": "22:00", "mood": "want somewhere quiet",
      "constraints": ["outdoors only"]}, "rain_85pct"),
    ({"city": "Bangalore", "budget": 500, "available_time": 45, "start_time": "11:00",
      "interests": ["art", "books", "shopping"], "constraints": ["outdoors only", "wheelchair accessible"]}, "unavailable"),
])
async def test_aud7_relaxation_holds_after_the_re_search(request_data: dict[str, Any], weather: str) -> None:
    deps = build_deps(fixture_settings(fixture_weather=weather))
    first = await run_baseline(PlanRequest(**request_data), deps)
    if first.status == "complete":
        assert first.plan is not None
        return  # Broader candidate discovery now satisfies the original request without relaxation.
    assert first.status == "no_feasible_plan" and first.infeasible is not None
    assert first.infeasible.relaxations
    failed = []
    for rel in first.infeasible.relaxations:
        again = await run_baseline(PlanRequest(**{**request_data, **rel.patch}), deps, asked_before=True)
        if again.status != "complete":
            failed.append((rel.label, again.message))
    assert not failed, failed
