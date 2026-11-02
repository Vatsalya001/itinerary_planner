"""Phase 1 re-verification (after the D-01..D-27 fixes): regressions and residual defects.

``xfail(strict=True)`` tests reproduce a defect confirmed during re-verification and documented in
docs/verification/PHASE1.md (section "Re-verification"); the reason starts with the defect ID (R-n for new
regressions/residuals). They assert the correct behaviour, so a fix turns them into XPASS failures that must then
be un-marked. Passing tests pin behaviour the fixes established. Synthetic data only; no network.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from app.baseline import run_baseline
from app.deps import build_deps, fixture_settings
from app.domain.constraints import map_constraint_text
from app.domain.parser_rules import apply_answers, clarification_questions, parse_rules
from app.domain.schemas import DraftStop, Evidence, PlanDraft, PlanRequest
from app.domain.sun import SAFETY_MARGIN_MIN, sun_times
from app.domain.travel import HeuristicTravel
from app.domain.validator import validate_plan
from app.providers.google_places import SEARCH_URL
from app.trace import Tracer
from tests.factories import make_candidate, make_ctx, make_ledger, make_prefs
from tests.verification.independent_checks import (
    Expect,
    constraint_keys_from_text,
    plan_failures,
    weather_fixture,
    world,
)
from tests.verification.test_phase1_adversarial import _london_google_deps, brief

NOW = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)
SAT = date(2026, 11, 7)
TRAVEL = HeuristicTravel()


def parse(text: str) -> Any:
    return parse_rules(PlanRequest(free_text=text), NOW)


# =====================================================================================================
# R-4 (P0): the D-21 negation fix drops every diet constraint when any diet word is negated
# =====================================================================================================


@pytest.mark.parametrize(("text", "key"), [
    ("no non-veg", "diet:vegetarian"),
    ("pure veg please, no non-veg places", "diet:vegetarian"),
    ("strictly vegetarian, never non veg", "diet:vegetarian"),
    ("I'm vegetarian, not vegan", "diet:vegetarian"),
    ("not a vegetarian place please, I'm vegan", "diet:vegan"),
])
def test_r4_negated_diet_words_keep_the_real_diet(text: str, key: str) -> None:
    assert key in {c.key for c in map_constraint_text(text)}


async def test_r4_no_non_veg_never_yields_a_non_veg_restaurant() -> None:
    deps = build_deps(fixture_settings())
    req = PlanRequest(city="Bangalore", budget=3000, available_time="4 hours", start_time="18:00",
                      mood="romantic, want a relaxed evening", interests=["food", "music"], constraints=["no non-veg"])
    first = await run_baseline(req, deps)
    if first.status == "needs_clarification":
        req = apply_answers(req, {"constraint": "Keep it as a preference"})
        result = await run_baseline(req, deps, asked_before=True)
    else:
        result = first
    assert result.plan is not None
    for s in result.plan.stops:
        assert s.candidate.diet.get("vegetarian") != Evidence.NO, f"{s.candidate.name} is non-vegetarian"


def test_d21_fixed_cases_still_hold() -> None:
    assert "diet:vegetarian" in {c.key for c in parse("Bangalore, I'm vegetarian; my friend is non-veg, 4 hours")
                                 .preferences.hard_constraints}
    assert "access:wheelchair" not in {c.key for c in map_constraint_text("easily accessible by metro")}
    assert "crowd:avoid" not in {c.key for c in map_constraint_text("not busy this weekend")}
    assert {c.key for c in map_constraint_text("vegan and jain")} == {"diet:vegan", "diet:jain"}


def test_r6_not_vegetarian_is_not_a_blocking_question() -> None:
    outcome = parse("Bangalore, I'm not vegetarian, 2000 rupees, 4 hours")
    assert clarification_questions(outcome, None, False) == []


# =====================================================================================================
# R-1 / R-2 (P1): time parsing after the dotted-time and early-start fixes
# =====================================================================================================


@pytest.mark.parametrize(("text", "start"), [
    ("Bangalore, from 6am for 3 hours, 1000 rupees", 6 * 60),
    ("Bangalore, 5am to 9am, 1000 rupees", 5 * 60),
])
def test_r1_explicit_am_is_never_moved_to_pm(text: str, start: int) -> None:
    assert parse(text).preferences.start_min == start


def test_r1_twelve_am_is_not_noon() -> None:
    assert parse("Bangalore, from 12am to 3am, 1000 rupees").preferences.start_min in {0, 24 * 60}


@pytest.mark.parametrize(("text", "start", "minutes"), [
    ("Bangalore, free 11.30 to 1.30, 1000 rupees", 11 * 60 + 30, 120),
    ("Bangalore, 9.30 to 1, 800 rupees", 9 * 60 + 30, 210),
    ("Bangalore, free 11:30 to 1:30, 1000 rupees", 11 * 60 + 30, 120),
])
def test_r2_ranges_across_noon(text: str, start: int, minutes: int) -> None:
    p = parse(text).preferences
    assert (p.start_min, p.available_minutes) == (start, minutes)


@pytest.mark.parametrize(("text", "start", "minutes"), [
    ("Bangalore, free from 4.30pm to 8pm, 1500 rupees", 16 * 60 + 30, 210),
    ("Bangalore, free 4.30 to 8.30 pm, 1500 rupees", 16 * 60 + 30, 240),
    ("Bangalore, from 1:00 to 4:00, 800 rupees", 13 * 60, 180),
    ("Bangalore, 10pm to 12am, 2000 rupees", 22 * 60, 120),
])
def test_d07_dotted_and_afternoon_times(text: str, start: int, minutes: int) -> None:
    p = parse(text).preferences
    assert (p.start_min, p.available_minutes) == (start, minutes)


@pytest.mark.parametrize("text", ["Mumbai, 2500 budget, 5 hours", "Mumbai, 2500 max, 5 hours", "Mumbai with 2500",
                                  "Mumbai, 2.5k budget", "Mumbai, can spend 2500"])
def test_d07_amount_phrasings(text: str) -> None:
    assert parse(text).preferences.budget == 2500


# =====================================================================================================
# R-3 (P1): the meal-window rule makes plans that start just before a meal window infeasible
# =====================================================================================================


async def test_r3_evening_start_before_the_dinner_window() -> None:
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(city="Bangalore", budget=1500, start_time="19:00", available_time="3 hours",
                                            mood="chill", interests=["walks", "nature"], constraints=["vegetarian"]),
                                deps)
    if result.status == "no_feasible_plan":
        reason = (result.message or "").lower()
        assert "dinner" in reason or "meal" in reason, result.message
    else:
        assert result.status == "complete"


async def test_r3_late_morning_wheelchair_request_is_not_told_to_drop_access() -> None:
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(city="Bangalore", budget=800, start_time="11:00", available_time="3 hours",
                                            mood="chill", interests=["food", "music", "walks"],
                                            constraints=["wheelchair accessible"]), deps)
    if result.status == "no_feasible_plan":
        assert result.infeasible is not None
        others = [r for r in result.infeasible.relaxations if "wheelchair" not in r.label.lower()]
        assert others, [r.label for r in result.infeasible.relaxations]
    else:
        assert result.status == "complete"


async def test_d09_brief_example_has_no_mid_afternoon_meal_and_dinner_is_in_window() -> None:
    deps = build_deps(fixture_settings())
    for req in (brief(), brief(mood="excited", available_time="6 hours", budget=3000, constraints=["no alcohol"],
                               interests=["food", "art", "walks"])):
        result = await run_baseline(req, deps)
        assert result.plan is not None
        for s in result.plan.stops:
            if s.candidate.category == "restaurant":
                assert 12 * 60 <= s.start_min < 14 * 60 + 30 or 19 * 60 + 30 <= s.start_min < 22 * 60, s


# =====================================================================================================
# R-5, R-7, R-8, R-9, D-22 residual (P2)
# =====================================================================================================


async def test_r5_shifted_start_has_a_single_start_assumption() -> None:
    deps = build_deps(fixture_settings(fixed_now=datetime(2026, 11, 7, 12, 30, tzinfo=UTC)))  # Sat 18:00 IST
    result = await run_baseline(PlanRequest(city="Bangalore", budget=2000, mood="chill", interests=["food"]), deps)
    assert result.plan is not None and result.plan.start_min >= 18 * 60
    starts = [a for a in result.assumptions if a.startswith("Starting at")]
    assert len(starts) == 1, starts


def test_r7_venue_names_are_not_clock_times() -> None:
    scoops = make_candidate("fx:scoops", "dessert", name="Midnight Scoops Dessert Bar")
    draft = PlanDraft(stops=[DraftStop(candidate_id="fx:scoops", duration_min=30,
                                       why="Midnight Scoops is a sweet, relaxed way to end the day.")])
    rep = validate_plan(draft, make_ledger(scoops), make_ctx(energy="medium"), make_prefs(energy="medium"), [], TRAVEL)
    assert "PROSE_NUMERIC_CLAIM" not in {v.code for v in rep.violations}


async def test_r8_price_filtered_search_is_not_reported_as_no_places() -> None:
    deps = _london_google_deps()
    request = brief(city="London", constraints=[])
    with respx.mock() as mock:
        mock.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"places": deps.extras["places"]}))
        answered = apply_answers(request, {"currency": "INR"})  # ₹2,000 is about £15
        result = await run_baseline(answered, deps, asked_before=True)
    await deps.http.aclose()  # type: ignore[union-attr]
    assert result.status == "no_feasible_plan"
    assert "no places or events were found" not in (result.message or "").lower(), result.message


async def test_r9_computed_sunset_keeps_the_safety_margin() -> None:
    deps = build_deps(fixture_settings(fixture_weather="unavailable"))
    from app.domain.parser_rules import apply_defaults, merge_parse
    from app.pipeline import build_context

    req = brief()
    outcome = merge_parse(None, parse_rules(req, deps.now()), req)
    [loc] = await deps.geocoder.resolve("Bangalore")
    ctx, _ = await build_context(apply_defaults(outcome, loc), loc, deps, Tracer("ctx"), deps.now())
    raw = sun_times(ctx.date, loc.lat, loc.lng, loc.timezone)
    assert raw is not None and ctx.weather.sunset_min is not None
    assert ctx.weather.sunset_min <= raw[1] - SAFETY_MARGIN_MIN


def test_r10_standing_only_restaurant_is_not_a_sit_down_meal() -> None:
    stand = make_candidate("fx:stand", "restaurant", seated=False)
    cons = map_constraint_text("include a sit-down meal")
    rep = validate_plan(make_draft_one("fx:stand", 45), make_ledger(stand), make_ctx(energy="medium"),
                        make_prefs(constraints=cons, energy="medium"), cons, TRAVEL)
    assert "MUST_INCLUDE_MISSING" in {v.code for v in rep.violations}


def make_draft_one(cid: str, minutes: int) -> PlanDraft:
    return PlanDraft(stops=[DraftStop(candidate_id=cid, duration_min=minutes, why="A good fit for you.")])


# =====================================================================================================
# Fixed behaviour pinned by re-verification
# =====================================================================================================


async def test_d01_foreign_budget_is_converted_and_disclosed() -> None:
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(city="Bangalore", budget=30, currency="USD", available_time="3 hours",
                                            interests=["coffee"], mood="chill"), deps)
    assert result.preferences is not None and result.preferences.currency == "INR"
    assert result.preferences.budget == 2850  # floor(30 * 95.0 / 1.0) with data/fx.yaml
    assert any("approximate reference rate" in a for a in result.assumptions)
    assert result.plan is not None and result.plan.totals.currency == "INR"


async def test_d01_london_inr_budget_is_planned_in_gbp_only() -> None:
    deps = _london_google_deps()
    request = brief(city="London", constraints=[], budget=20000)
    with respx.mock() as mock:
        mock.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"places": deps.extras["places"]}))
        result = await run_baseline(apply_answers(request, {"currency": "INR"}), deps, asked_before=True)
    await deps.http.aclose()  # type: ignore[union-attr]
    assert result.status == "complete" and result.plan is not None
    assert result.preferences is not None and result.preferences.currency == "GBP"
    currencies = {s.candidate.cost.currency for s in result.plan.stops} | {leg.fare.currency for leg in result.plan.legs}
    assert currencies <= {"GBP"} and result.plan.totals.currency == "GBP"


@pytest.mark.parametrize("loc_id", ["fixture:bangalore", "fixture:mumbai", "fixture:london", "fixture:jaipur"])
def test_d04_computed_sunset_is_close_to_the_fixture(loc_id: str) -> None:
    loc = next(x for x in world()["locations"] if x["id"] == loc_id)
    times = sun_times(SAT, loc["lat"], loc["lng"], loc["timezone"])
    assert times is not None
    assert abs(times[1] - weather_fixture()["scenarios"]["clear"][loc_id]["sunset_min"]) <= 10


def _grid(n: int, seed: int) -> list[tuple[str, dict[str, Any]]]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        req: dict[str, Any] = {
            "city": rng.choice(["Bangalore", "Bangalore", "Mumbai"]),
            "available_time": rng.choice(["2 hours", "3 hours", "4 hours", "6 hours", "9 hours"]),
            "mood": rng.choice(["tired", "", "energetic, want to pack it in", "chill", "romantic evening"]),
            "interests": rng.choice([["food", "music", "walks"], ["coffee", "books"], ["art", "museums"],
                                     ["walks", "nature"], ["live music"], ["food"], []]),
            "constraints": rng.choice([[], ["vegetarian"], ["vegetarian", "avoid crowded places"],
                                       ["wheelchair accessible"], ["vegan"], ["no alcohol"], ["indoors only"]]),
            "budget": rng.choice([300, 800, 1500, 3000]),
        }
        start = rng.choice([None, "09:00", "11:00", "13:00", "15:00", "17:00", "19:00", "21:00"])
        if start:
            req["start_time"] = start
        out.append((rng.choice(["clear", "clear", "rain_85pct", "unavailable"]), req))
    return out


async def test_random_requests_pass_every_independent_check() -> None:
    """300 seeded random requests: every complete plan passes the independent recomputation (cost, legs, time,
    hours, evidence, pacing, prose, daylight with the true sunset even when weather is unavailable, Plan B)."""
    deps = {w: build_deps(fixture_settings(fixture_weather=w)) for w in ("clear", "rain_85pct", "unavailable")}
    locs = {"Bangalore": "fixture:bangalore", "Mumbai": "fixture:mumbai"}
    failures: list[str] = []
    complete = 0
    for weather, req in _grid(300, seed=29):
        result = await run_baseline(PlanRequest(**req), deps[weather])
        if result.status != "complete":
            continue
        complete += 1
        minutes = int(float(req["available_time"].split()[0]) * 60)
        if "start_time" in req:
            hh, mm = req["start_time"].split(":")
            start = int(hh) * 60 + int(mm)
        else:
            start = 600 if minutes > 480 else 900
        exp = Expect(location_id=locs[req["city"]], budget=req["budget"], start_min=start, available=minutes,
                     constraints=constraint_keys_from_text(req["constraints"]), weather=weather)
        failures += [f"{req} [{weather}]: {f}" for f in plan_failures(result, exp)]
    assert complete > 200
    assert not failures, failures[:5]
