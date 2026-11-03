"""Independent audit (core): deterministic planner, fill and infeasibility explanations.

Each ``xfail(strict=True)`` test reproduces a confirmed bug (docs/verification/AUDIT_CORE.md, AUD-<n>). Offline only.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.domain.fallback as fallback_mod
from app.baseline import run_baseline
from app.deps import Deps, build_deps, fixture_settings
from app.domain.evidence import is_full_meal, is_outdoor, is_walking_stop, meal_slot
from app.domain.fallback import search_plans
from app.domain.parser_rules import apply_defaults, default_queries, merge_parse, parse_rules
from app.domain.schemas import Candidate, Constraint, PlanRequest, PlanResult, Preferences, WorldContext
from app.pipeline import build_context, enforced_constraints, search_events, search_places
from app.trace import Tracer


async def world_for(request: dict[str, Any], weather: str = "clear",
                    **settings: Any) -> tuple[list[Candidate], WorldContext, Preferences, list[Constraint], Deps]:
    """The baseline's own pool, context, preferences and constraints for ``request`` (fixture providers)."""
    deps = build_deps(fixture_settings(fixture_weather=weather, **settings))
    req = PlanRequest(**request)
    now, tracer = deps.now(), Tracer("audit")
    outcome = merge_parse(None, parse_rules(req, now), req)
    assert outcome.preferences.city
    location = (await deps.geocoder.resolve(outcome.preferences.city))[0]
    prefs = apply_defaults(outcome, location)
    ctx, _ = await build_context(prefs, location, deps, tracer, now)
    constraints, _original = enforced_constraints(prefs, req)
    places, _, _ = await search_places(default_queries(prefs, ctx), ctx, deps, tracer)
    events, _ = await search_events(prefs, ctx, deps, tracer)
    return places + events, ctx, prefs, constraints, deps


async def apply_each_relaxation(request: dict[str, Any], result: PlanResult, deps: Deps) -> list[tuple[str, str]]:
    """(label, status) after applying each one-tap relaxation the way a client would (shallow patch)."""
    assert result.infeasible is not None
    out: list[tuple[str, str]] = []
    for rel in result.infeasible.relaxations:
        patched = {**request, **rel.patch}
        again = await run_baseline(PlanRequest(**patched), deps, asked_before=True)
        out.append((rel.label, again.status))
    return out


# ---------------------------------------------------------------------------------------------------
# AUD-4 (P1): the "best" beam search misses valid plans that need shorter-than-default durations
# ---------------------------------------------------------------------------------------------------

BEAM_CASES = [
    # 2 h window, live music opens at 16:00: Freedom Park for 30 min then Lavelle Road Music Room works
    ({"city": "Bangalore", "budget": 615, "available_time": 120, "start_time": "15:00",
      "mood": "energetic, want to pack a lot in", "interests": ["nature", "food", "dessert"],
      "constraints": ["vegan", "include live music"]}, "clear"),
    # Three must-includes in Mumbai, rain. Cab pickup/transfer bounds replace the old auto estimates;
    # allow 190 min so this remains a feasible beam-search completeness regression, not a transport test.
    ({"city": "Mumbai", "budget": 10000, "available_time": 190, "start_time": "11:30", "mood": "feeling okay",
      "interests": ["museums"], "constraints": ["include live music", "must include an outdoor walk",
                                                 "must include a museum"]}, "rain_85pct"),
    # sit-down meal + museum in 110 min
    ({"city": "Bangalore", "budget": 3333, "available_time": 110, "start_time": "11:00",
      "interests": ["music", "shopping", "street food"],
      "constraints": ["no malls", "include a sit-down meal", "must include a museum"]}, "unavailable"),
]


@pytest.mark.parametrize(("request_data", "weather"), BEAM_CASES)
async def test_aud4_best_finds_a_plan_whenever_cheapest_does(request_data: dict[str, Any], weather: str) -> None:
    pool, ctx, prefs, constraints, deps = await world_for(request_data, weather)
    cheapest = search_plans(pool, ctx, prefs, constraints, deps.travel, objective="cheapest")
    assert cheapest is not None and cheapest.report.ok  # a valid plan exists under every rule
    best = search_plans(pool, ctx, prefs, constraints, deps.travel, objective="best")
    assert best is not None, "the baseline reports no_feasible_plan although a valid plan exists"


async def test_aud4_offered_relaxations_produce_a_plan() -> None:
    request = {"city": "Bangalore", "budget": 333, "available_time": 120, "start_time": "15:00",
               "mood": "energetic, want to pack a lot in", "interests": ["nature", "food", "dessert"],
               "constraints": ["vegan", "include live music"]}
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(**request), deps)
    assert result.status == "no_feasible_plan"
    outcomes = await apply_each_relaxation(request, result, deps)
    failed = [label for label, status in outcomes if status != "complete"]
    assert not failed, f"one-tap relaxations that still give no plan: {failed}"


# ---------------------------------------------------------------------------------------------------
# AUD-8 (P1): "outdoors only" on a rainy day spanning a meal: default_queries searches no outdoor category
# ---------------------------------------------------------------------------------------------------

async def test_aud8_outdoors_only_in_rain_searches_outdoor_places() -> None:
    request = {"city": "Bangalore", "budget": 1000, "available_time": 360, "start_time": "11:00",
               "interests": ["food", "art"], "constraints": ["outdoors only"]}
    pool, *_ = await world_for(request, "rain_85pct")
    assert any(is_outdoor(c) for c in pool), "no outdoor candidate was searched for an outdoors-only request"
    deps = build_deps(fixture_settings(fixture_weather="rain_85pct"))
    result = await run_baseline(PlanRequest(**request), deps)
    assert result.status == "complete", result.message


# ---------------------------------------------------------------------------------------------------
# AUD-13 (P2): fill_window stretches sit-down meals out of their meal window (validator has no meal rule)
# ---------------------------------------------------------------------------------------------------

FILL_MEAL_CASES = [
    ({"city": "Bangalore", "budget": 1000, "available_time": 720, "start_time": "14:00", "interests": ["coffee"]}, "clear"),
    ({"city": "Bombay", "budget": 777, "available_time": 600, "start_time": "10:00", "mood": "adventurous",
      "constraints": ["must include a museum", "no alcohol"]}, "rain_85pct"),
]


@pytest.mark.parametrize(("request_data", "weather"), FILL_MEAL_CASES)
async def test_aud13_meals_stay_in_meal_windows_after_fill(request_data: dict[str, Any], weather: str) -> None:
    deps = build_deps(fixture_settings(fixture_weather=weather))
    result = await run_baseline(PlanRequest(**request_data), deps)
    assert result.plan is not None
    off = [(s.candidate.name, s.start_min, s.end_min) for s in result.plan.stops
           if is_full_meal(s.candidate) and meal_slot(s.start_min, s.end_min) is None]
    assert off == [], f"sit-down meals outside breakfast/lunch/dinner: {off}"


async def test_aud13_control_without_fill_meals_are_timed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: with fill_window disabled the planner's own stretch keeps every meal in its window."""
    monkeypatch.setattr(fallback_mod, "fill_window", lambda draft, *a, **k: draft)
    for request_data, weather in FILL_MEAL_CASES:
        deps = build_deps(fixture_settings(fixture_weather=weather))
        result = await run_baseline(PlanRequest(**request_data), deps)
        assert result.plan is not None
        assert all(meal_slot(s.start_min, s.end_min) is not None for s in result.plan.stops if is_full_meal(s.candidate))


async def test_aud13_unknown_hours_stop_stays_in_daytime_after_fill() -> None:
    deps = build_deps(fixture_settings(places_provider="osm_snapshot", events_provider="none"))
    result = await run_baseline(PlanRequest(city="Bangalore", budget=1000, available_time=240, start_time="20:00",
                                            interests=["books", "coffee"]), deps)
    assert result.plan is not None
    late = [(s.candidate.name, s.end_min) for s in result.plan.stops
            if s.candidate.kind == "place" and s.candidate.hours.status != "known"
            and s.candidate.category not in {"bar", "brewery", "live_music_venue", "theatre", "cinema", "game_arcade"}
            and s.end_min > fallback_mod.UNKNOWN_HOURS_DAY[1]]
    assert late == [], f"unknown-hours daytime places scheduled past 22:00: {late}"


# ---------------------------------------------------------------------------------------------------
# AUD-14 (P2): outdoor walks are allowed before sunrise (only sunset is checked)
# ---------------------------------------------------------------------------------------------------

async def test_aud14_no_outdoor_walk_before_sunrise() -> None:
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(city="Bangalore", budget=1000, available_time=90, start_time="05:30",
                                            interests=["walks"]), deps)
    assert result.plan is not None
    sunrise = result.plan.weather.sunrise_min
    assert sunrise is not None
    dark = [(s.candidate.name, s.start_min) for s in result.plan.stops
            if is_walking_stop(s.candidate) and is_outdoor(s.candidate) and s.start_min < sunrise]
    assert dark == [], f"outdoor walks starting before sunrise ({sunrise}): {dark}"


# ---------------------------------------------------------------------------------------------------
# AUD-22 (P2): baseline search ignores time of day / budget: no plan although an easy one exists
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("request_data", [
    # 23:00, ₹500: Midnight Scoops (dessert, open to 01:00, ₹120-280) alone is a valid plan
    {"city": "Bangalore", "budget": 500, "available_time": 300, "start_time": "23:00"},
    # ₹0, indoors only, 17:00: Rangoli Metro Art Center is free and open to 20:00
    {"city": "Bangalore", "budget": 0, "available_time": 480, "start_time": "17:00", "interests": ["history"],
     "constraints": ["indoors only"]},
])
async def test_aud22_easy_single_stop_plan_is_found(request_data: dict[str, Any]) -> None:
    deps = build_deps(fixture_settings(fixture_weather="rain_85pct" if request_data["budget"] == 0 else "clear"))
    result = await run_baseline(PlanRequest(**request_data), deps)
    assert result.status == "complete", result.message
