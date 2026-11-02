import time
from typing import Any

import pytest

from app.domain.evidence import is_food_stop, is_meal_stop, is_outdoor, likely_busy
from app.domain.fallback import plan_deterministic, prefilter
from app.domain.schemas import Evidence, Hours, PlanDraft
from app.domain.travel import HeuristicTravel
from app.domain.validator import numeric_claims, validate_plan
from tests.factories import (
    bangalore_like_world,
    make_candidate,
    make_constraints,
    make_ctx,
    make_ledger,
    make_prefs,
    synthetic_candidates,
)

travel = HeuristicTravel()
WORLD = bangalore_like_world()


def plan_and_validate(candidates, ctx, prefs):
    draft = plan_deterministic(candidates, ctx, prefs, prefs.hard_constraints, travel)
    report = validate_plan(draft, make_ledger(*candidates), ctx, prefs, prefs.hard_constraints, travel) if draft else None
    return draft, report


def assert_no_numbers(draft: PlanDraft) -> None:
    texts = [s.why for s in draft.stops] + [s.tradeoff or "" for s in draft.stops] + [draft.summary_note]
    texts += [o.reason for o in draft.omitted]
    for text in texts:
        assert numeric_claims(text) == [], text
        assert not any(ch.isdigit() for ch in text), text


def test_tired_vegetarian_avoid_crowds_assignment_example():
    ctx = make_ctx(energy="low")
    prefs = make_prefs(budget=2000, available=240, energy="low",
                       constraints=["vegetarian", "avoid crowded places"], interests=["food", "music", "walks"])
    draft, report = plan_and_validate(WORLD, ctx, prefs)
    assert draft is not None and report is not None
    assert report.ok, report.violations
    plan = report.plan
    assert plan is not None
    assert 1 <= len(plan.stops) <= 3
    assert any(s.candidate.seated for s in plan.stops)
    assert plan.totals.worst_case <= 2000
    assert plan.totals.elapsed_min <= 240
    assert plan.totals.elapsed_min >= 0.6 * 240  # uses most of the window
    for s in plan.stops:
        if is_food_stop(s.candidate):
            assert s.candidate.diet.get("vegetarian") == Evidence.CONFIRMED
        assert not likely_busy(s.candidate, s.start_min, s.end_min)
        assert s.why.strip()
    ids = {s.candidate.id for s in plan.stops}
    decoys = {"fx:busy_brewpub", "fx:packed_cafe", "fx:kebab_corner", "fx:late_opener", "fx:mystery_diner", "fx:far_lake"}
    assert not ids & decoys
    assert_no_numbers(draft)


def test_covers_distinct_interests():
    ctx = make_ctx(energy="low")
    prefs = make_prefs(constraints=["vegetarian", "avoid crowded places"], interests=["food", "music", "walks"])
    _, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.plan is not None
    tags = set()
    for s in report.plan.stops:
        tags |= set(s.candidate.tags)
    assert {"food", "music"} <= tags
    assert tags & {"walks", "nature", "outdoors"}


SCENARIOS: dict[str, dict[str, dict[str, Any]]] = {
    "low_default": dict(ctx=dict(energy="low"), prefs=dict(energy="low", constraints=["vegetarian", "avoid crowded places"])),
    "medium_no_constraints": dict(ctx=dict(energy="medium"), prefs=dict(energy="medium")),
    "high_long_day": dict(ctx=dict(energy="high", start_min=600, available=540),
                          prefs=dict(energy="high", start_min=600, available=540, budget=4000)),
    "rain": dict(ctx=dict(energy="low", weather="rain"), prefs=dict(energy="low", constraints=["vegetarian"])),
    "weather_unavailable": dict(ctx=dict(energy="low", weather="unavailable"), prefs=dict(energy="low")),
    "wheelchair_veg": dict(ctx=dict(energy="low"),
                           prefs=dict(energy="low", constraints=["wheelchair accessible", "vegetarian"])),
    "tight_budget": dict(ctx=dict(energy="medium"),
                         prefs=dict(energy="medium", budget=300, interests=["walks", "art"])),
    "date_night": dict(ctx=dict(energy="low", start_min=1080), prefs=dict(energy="low", start_min=1080, budget=3000,
                                                                          interests=["food", "music"])),
    "must_include_books": dict(ctx=dict(energy="medium"), prefs=dict(energy="medium", constraints=["must_include:books"])),
    "no_alcohol": dict(ctx=dict(energy="medium"), prefs=dict(energy="medium", constraints=["alcohol:avoid"])),
    "indoors_only": dict(ctx=dict(energy="medium"), prefs=dict(energy="medium", constraints=["indoors only"])),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_returned_draft_validates(name):
    spec = SCENARIOS[name]
    ctx = make_ctx(**spec["ctx"])
    prefs = make_prefs(**spec["prefs"])
    draft, report = plan_and_validate(WORLD, ctx, prefs)
    assert draft is not None, name
    assert report is not None and report.ok, report.violations
    assert report.plan is not None
    assert len(report.plan.stops) <= ctx.pacing.max_stops
    assert_no_numbers(draft)


def test_rain_gives_outdoor_stops_an_indoor_plan_b():
    ctx = make_ctx(energy="medium", weather="rain")
    prefs = make_prefs(energy="medium", interests=["walks"], constraints=[])
    _, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.ok and report.plan is not None
    for s in report.plan.stops:
        if is_outdoor(s.candidate):
            assert s.plan_b is not None and s.plan_b.indoor is True
    assert "WEATHER_RISK" not in {v.code for v in report.violations}


def test_rain_with_only_outdoor_walks_still_offers_plan_b():
    park = make_candidate("fx:park", "park")
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.3, 0.0))
    museum = make_candidate("fx:museum", "museum", offset_km=(0.6, 0.0), cost=(20, 50))
    gallery = make_candidate("fx:gallery", "art_gallery", offset_km=(0.2, 0.2), cost=(0, 50))
    ctx = make_ctx(energy="low", weather="rain")
    prefs = make_prefs(energy="low", interests=["walks"], constraints=["must_include:outdoor_walk"])
    draft, report = plan_and_validate([park, cafe, museum, gallery], ctx, prefs)
    assert draft is not None and report is not None and report.ok
    park_stop = next(s for s in draft.stops if s.candidate_id == "fx:park")
    used = {s.candidate_id for s in draft.stops}
    assert park_stop.plan_b_candidate_id in {"fx:cafe", "fx:museum", "fx:gallery"} - used
    assert "WEATHER_RISK" not in {v.code for v in report.violations}


def test_wheelchair_excludes_unknown_access():
    ctx = make_ctx(energy="low")
    prefs = make_prefs(constraints=["wheelchair accessible", "vegetarian"])
    _, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.plan is not None
    assert all(s.candidate.wheelchair == Evidence.CONFIRMED for s in report.plan.stops)


def test_dinner_inside_the_dinner_window():
    ctx = make_ctx(energy="medium", start_min=1080)
    prefs = make_prefs(energy="medium", start_min=1080, budget=3000, interests=["food", "music"], constraints=[])
    _, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.plan is not None
    meals = [s for s in report.plan.stops if is_meal_stop(s.candidate)]
    assert meals
    assert any(s.start_min < 22 * 60 and s.end_min > 19 * 60 + 30 for s in meals)


def test_meal_in_lunch_window_on_long_day():
    ctx = make_ctx(energy="high", start_min=600, available=540)
    prefs = make_prefs(energy="high", start_min=600, available=540, budget=4000)
    _, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.plan is not None
    assert 4 <= len(report.plan.stops) <= 6
    assert any(is_meal_stop(s.candidate) and s.start_min < 870 and s.end_min > 720 for s in report.plan.stops)


def test_must_include_is_respected():
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=["must_include:sit_down_meal"])
    _, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.plan is not None
    assert any(is_meal_stop(s.candidate) for s in report.plan.stops)


def test_returns_none_when_impossible():
    ctx = make_ctx(energy="low")
    prefs = make_prefs(budget=1, constraints=["vegetarian", "wheelchair accessible", "include a sit-down meal"])
    assert plan_deterministic(WORLD, ctx, prefs, prefs.hard_constraints, travel) is None
    assert plan_deterministic([], ctx, make_prefs(), [], travel) is None
    closed = [make_candidate(f"fx:c{i}", "cafe", hours=Hours(status="known", periods=[])) for i in range(3)]
    assert plan_deterministic(closed, ctx, make_prefs(), [], travel) is None


def test_constraints_argument_is_enforced_even_if_prefs_lost_it():
    nonveg = make_candidate("fx:nv", "restaurant", diet={"vegetarian": Evidence.NO}, rating=5.0)
    veg = make_candidate("fx:v", "cafe", offset_km=(0.3, 0.0))
    prefs = make_prefs(constraints=[])
    draft = plan_deterministic([nonveg, veg], make_ctx(), prefs, make_constraints(["vegetarian"]), travel)
    assert draft is not None
    assert "fx:nv" not in {s.candidate_id for s in draft.stops}


def test_deterministic_output():
    ctx = make_ctx()
    prefs = make_prefs(constraints=["vegetarian"])
    a = plan_deterministic(WORLD, ctx, prefs, prefs.hard_constraints, travel)
    b = plan_deterministic(list(reversed(WORLD)), ctx, prefs, prefs.hard_constraints, travel)
    assert a is not None and a == b


def test_prefilter_reasons():
    ctx = make_ctx()
    cons = make_constraints(["vegetarian", "avoid crowded places"])
    kept, excluded = prefilter(WORLD, ctx, make_prefs(), cons, budget=2000)
    assert "fx:kebab_corner" in excluded and "diet" in excluded["fx:kebab_corner"]
    assert "fx:mystery_diner" in excluded
    assert "closed" in excluded["fx:late_opener"]
    assert "crowd" in excluded["fx:packed_cafe"]
    assert "fx:quiet_cafe" in {c.id for c in kept}


@pytest.mark.parametrize(("energy", "start", "available", "budget"),
                         [("low", 900, 240, 2000), ("medium", 900, 240, 2000), ("high", 600, 600, None)])
def test_performance_sixty_candidates(energy, start, available, budget):
    candidates = synthetic_candidates(60)
    ctx = make_ctx(energy=energy, start_min=start, available=available)
    prefs = make_prefs(energy=energy, start_min=start, available=available, budget=budget,
                       constraints=["vegetarian", "avoid crowded places"])
    t0 = time.perf_counter()
    draft = plan_deterministic(candidates, ctx, prefs, prefs.hard_constraints, travel)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.5, elapsed
    assert draft is not None
    report = validate_plan(draft, make_ledger(*candidates), ctx, prefs, prefs.hard_constraints, travel)
    assert report.ok, report.violations
    assert_no_numbers(draft)
