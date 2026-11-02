import itertools
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


# --- verification follow-ups (D-06, D-09, D-10, D-11, D-15, D-16, D-27 and plan quality) ------------------


def test_brief_example_has_no_full_meal_mid_afternoon():
    for weather in ("clear", "rain"):
        ctx = make_ctx(energy="low", weather=weather)
        prefs = make_prefs(constraints=["vegetarian", "avoid crowded places"])
        draft, report = plan_and_validate(WORLD, ctx, prefs)
        assert report is not None and report.ok and report.plan is not None
        for s in report.plan.stops:
            assert not (is_meal_stop(s.candidate) and s.candidate.seated), (s.candidate.id, s.start_min)
        assert draft is not None
        assert all("sensible time" not in s.why for s in draft.stops)


def test_full_meal_must_sit_inside_a_meal_window():
    from app.domain.evidence import main_meal_window, meal_slot

    ctx = make_ctx(energy="medium", start_min=900, available=420)  # 15:00-22:00, family-day shape
    prefs = make_prefs(energy="medium", start_min=900, available=420, budget=1500, constraints=["vegetarian"])
    draft, report = plan_and_validate(WORLD, ctx, prefs)
    assert draft is not None and report is not None and report.plan is not None
    meals = [s for s in report.plan.stops if is_meal_stop(s.candidate) and s.candidate.seated]
    assert meals, "a dinner should be planned when the window spans dinner"
    for s in meals:
        assert meal_slot(s.start_min, s.end_min) is not None
    for s, d in zip(report.plan.stops, draft.stops, strict=True):
        if "sensible time" in d.why:
            assert main_meal_window(s.start_min, s.end_min) is not None


def test_required_meal_is_still_planned_outside_meal_windows():
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=["must_include:sit_down_meal"])
    draft, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.ok and draft is not None
    assert all("sensible time" not in s.why for s in draft.stops)


def test_event_waits_are_capped():
    cafe = make_candidate("fx:cafe", "cafe")
    gig = make_candidate("ev:gig", "event", offset_km=(0.3, 0.0), event_start_min=1080, event_end_min=1200)
    ctx = make_ctx(energy="medium", available=420)
    prefs = make_prefs(energy="medium", available=420, interests=["music"])
    _draft, report = plan_and_validate([cafe, gig], ctx, prefs)
    assert report is not None and report.plan is not None
    plan = report.plan
    for i in range(1, len(plan.stops)):
        gap = plan.stops[i].start_min - plan.stops[i - 1].end_min - plan.legs[i - 1].minutes_max
        assert gap <= ctx.pacing.buffer_minutes + 30


def test_no_back_to_back_same_category():
    cafes = [make_candidate(f"fx:cafe{i}", "cafe", offset_km=(0.2 * i, 0.0), tags=["coffee", "food"])
             for i in range(3)]
    books = make_candidate("fx:books", "bookstore", offset_km=(0.3, 0.3))
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", interests=["coffee"], constraints=[])
    _, report = plan_and_validate([*cafes, books], ctx, prefs)
    assert report is not None and report.plan is not None
    cats = [s.candidate.category for s in report.plan.stops]
    assert all(a != b for a, b in itertools.pairwise(cats)), cats


def test_programme_venue_with_unknown_hours_is_not_a_standalone_stop():
    hall = make_candidate("fx:hall", "live_music_venue", hours=Hours(), rating=5.0, live_music=True)
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.3, 0.0))
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", interests=["music"], constraints=[])
    draft, _report = plan_and_validate([hall, cafe], ctx, prefs)
    assert draft is not None and "fx:hall" not in {s.candidate_id for s in draft.stops}
    _, excluded = prefilter([hall], ctx, prefs, [], budget=None)
    assert excluded["fx:hall"] == ["programme"]


def test_low_energy_durations_stay_moderate():
    ctx = make_ctx(energy="low")
    prefs = make_prefs(constraints=["vegetarian"])
    _draft, report = plan_and_validate(WORLD, ctx, prefs)
    assert report is not None and report.plan is not None
    at_max = [s for s in report.plan.stops if s.candidate.kind == "place" and s.duration_min == s.candidate.duration_range[2]]
    assert len(at_max) < len(report.plan.stops), [(s.candidate.id, s.duration_min) for s in report.plan.stops]
    assert report.plan.totals.elapsed_min >= 0.6 * 240


def test_outdoors_only_chill_morning_is_feasible_with_a_note():
    walks = [make_candidate(f"fx:w{i}", key, offset_km=(0.4 * i, 0.0)) for i, key in
             enumerate(["park", "garden", "lake"])]
    ctx = make_ctx(energy="low", start_min=600)
    prefs = make_prefs(energy="low", start_min=600, interests=["walks", "nature"], constraints=["outdoors only"])
    draft, report = plan_and_validate(walks, ctx, prefs)
    assert draft is not None and report is not None and report.ok
    assert "PACING_RELAXED" in {v.code for v in report.violations}
    assert "rests" in draft.summary_note


def test_rain_plan_b_is_open_distinct_and_within_budget():
    park = make_candidate("fx:park", "park", cost=0)
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.3, 0.0), cost=(150, 300))
    closed_museum = make_candidate("fx:closed", "museum", offset_km=(0.1, 0.0),
                                   hours=Hours(status="known", periods=[(1140, 1260)]))
    spa = make_candidate("fx:spa", "spa", offset_km=(0.1, 0.1), cost=(1500, 2500))
    gallery = make_candidate("fx:gallery", "art_gallery", offset_km=(0.5, 0.2), cost=(0, 50))
    ctx = make_ctx(energy="medium", weather="rain")
    prefs = make_prefs(energy="medium", budget=900, interests=["walks"], constraints=["must_include:outdoor_walk"])
    draft, report = plan_and_validate([park, cafe, closed_museum, spa, gallery], ctx, prefs)
    assert draft is not None and report is not None and report.ok, report and report.violations
    park_stop = next(s for s in draft.stops if s.candidate_id == "fx:park")
    stop_ids = {s.candidate_id for s in draft.stops}
    assert park_stop.plan_b_candidate_id not in {None, "fx:closed", "fx:spa"} | stop_ids


def test_mood_romantic_prefers_romantic_tagged_places():
    plain = make_candidate("fx:plain", "restaurant", rating=4.6, tags=["food"])
    romantic = make_candidate("fx:romantic", "restaurant", offset_km=(0.4, 0.0), rating=4.4, tags=["food", "romantic"])
    ctx = make_ctx(energy="medium", start_min=1170, available=150)
    prefs = make_prefs(energy="medium", start_min=1170, available=150, budget=3000, constraints=[],
                       mood="romantic evening with my partner", interests=["food"])
    draft, _ = plan_and_validate([plain, romantic], ctx, prefs)
    assert draft is not None and draft.stops[0].candidate_id == "fx:romantic"


def test_prefers_events_with_real_coordinates():
    exact = make_candidate("ev:exact", "event", tags=["music"], rating=4.2)
    approx = make_candidate("ev:approx", "event", tags=["music", "approx_location"], rating=4.8)
    ctx = make_ctx(energy="medium", start_min=1020, available=150)
    prefs = make_prefs(energy="medium", start_min=1020, available=150, interests=["music"], constraints=[])
    draft, _ = plan_and_validate([exact, approx], ctx, prefs)
    assert draft is not None and draft.stops[0].candidate_id == "ev:exact"


def test_template_claims_are_backed():
    ctx = make_ctx(energy="low")
    prefs = make_prefs(constraints=["vegetarian", "avoid crowded places"])
    draft, report = plan_and_validate(WORLD, ctx, prefs)
    assert draft is not None and report is not None and report.plan is not None
    for s, d in zip(report.plan.stops, draft.stops, strict=True):
        if "usually calm" in d.why:
            c = s.candidate
            assert c.popularity == "low"
            assert not any(s.start_min < we and ws < s.end_min for ws, we in c.busy_windows)
    leads = [d.why.split(";")[0] for d in draft.stops]
    assert len(set(leads)) == len(leads), leads
    no_food = [make_candidate("fx:g", "art_gallery"), make_candidate("fx:b", "bookstore", offset_km=(0.3, 0.0)),
               make_candidate("fx:c", "cafe", offset_km=(0.6, 0.0), diet={})]
    d2, _ = plan_and_validate(no_food, make_ctx(energy="medium"), make_prefs(energy="medium", constraints=["vegetarian"]))
    assert d2 is not None and "food stop" not in d2.summary_note


def test_currency_prefilter():
    gbp = make_candidate("fx:gbp", "cafe")
    gbp = gbp.model_copy(update={"cost": gbp.cost.model_copy(update={"currency": "GBP"})})
    _, excluded = prefilter([gbp], make_ctx(), make_prefs(), [], budget=2000)
    assert excluded["fx:gbp"] == ["currency"]


def test_quiet_mood_names_the_music_tension():
    room = make_candidate("fx:room", "live_music_venue", popularity="low", live_music=True,
                          hours=Hours(status="known", periods=[(900, 1380)]))
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.3, 0.0))
    ctx = make_ctx(energy="low")
    prefs = make_prefs(mood="want somewhere quiet", interests=["live music"], constraints=[])
    draft, _ = plan_and_validate([room, cafe], ctx, prefs)
    assert draft is not None
    notes = " ".join(s.tradeoff or "" for s in draft.stops).lower()
    assert "quiet" in notes


# --- stated order (D-12) ---------------------------------------------------------------------------


def _brunch_world() -> list:
    return [
        make_candidate("fx:gallery", "art_gallery", rating=4.9, cost=(0, 100)),
        make_candidate("fx:brunch", "cafe", offset_km=(0.4, 0.0), tags=["food", "coffee", "brunch"], rating=4.3),
        make_candidate("fx:plain_cafe", "cafe", offset_km=(0.2, 0.3), tags=["food", "coffee"], rating=4.5),
        make_candidate("fx:museum", "museum", offset_km=(0.8, 0.2), cost=(20, 100)),
    ]


def test_stated_order_is_followed():
    from app.domain.fallback import order_items

    ctx = make_ctx(energy="medium", start_min=660, available=240)
    prefs = make_prefs(energy="medium", start_min=660, available=240, budget=1200, constraints=[],
                       interests=["food", "art"], soft_preferences=["order: brunch > art"])
    assert order_items(prefs) == ["brunch", "art"]
    draft, report = plan_and_validate(_brunch_world(), ctx, prefs)
    assert draft is not None and report is not None and report.ok and report.plan is not None
    first = report.plan.stops[0]
    assert first.candidate.id == "fx:brunch" and first.start_min == 660
    assert "brunch first, as you asked" in draft.stops[0].why
    assert report.plan.stops[1].candidate.category in {"art_gallery", "museum"}
    assert report.plan.end_min <= 15 * 60
    # without the stated order the higher-rated gallery may lead
    unordered = make_prefs(energy="medium", start_min=660, available=240, budget=1200, constraints=[],
                           interests=["food", "art"])
    d2, _ = plan_and_validate(_brunch_world(), ctx, unordered)
    assert d2 is not None and all("as you asked" not in s.why for s in d2.stops)


def test_brunch_restaurant_counts_as_a_timed_meal_in_the_morning():
    resto = make_candidate("fx:resto", "restaurant", tags=["food", "brunch"], rating=4.5)
    gallery = make_candidate("fx:gallery", "art_gallery", offset_km=(0.4, 0.0))
    ctx = make_ctx(energy="medium", start_min=660, available=240)
    prefs = make_prefs(energy="medium", start_min=660, available=240, budget=1500, constraints=[],
                       interests=["food", "art"], soft_preferences=["order: brunch > art"])
    draft, report = plan_and_validate([resto, gallery], ctx, prefs)
    assert draft is not None and report is not None and report.plan is not None
    assert [s.candidate.id for s in report.plan.stops][:2] == ["fx:resto", "fx:gallery"]
    # a brunch item never matches an afternoon stop
    from app.domain.fallback import order_match_at, order_match_level

    assert order_match_at(order_match_level(resto, "brunch"), "brunch", 15 * 60) == 0


# --- re-verification follow-ups (R-3, R-11, D-12 residual) ------------------------------------------------


def test_evening_start_can_open_with_an_early_dinner():
    resto = make_candidate("fx:resto", "restaurant")
    ctx = make_ctx(energy="low", start_min=1140, available=180)
    prefs = make_prefs(start_min=1140, available=180, constraints=["vegetarian"], interests=["walks"])
    draft, report = plan_and_validate([resto], ctx, prefs)
    assert draft is not None and report is not None and report.ok
    assert "an early dinner" in draft.stops[0].why and "sensible time" not in draft.stops[0].why


def test_reordered_request_gets_a_trade_off():
    cafe = make_candidate("fx:cafe", "cafe", cost=(100, 300))
    room = make_candidate("fx:room", "live_music_venue", offset_km=(0.3, 0.0), live_music=True, cost=(200, 500),
                          hours=Hours(status="known", periods=[(960, 1380)]))
    resto = make_candidate("fx:resto", "restaurant", offset_km=(0.6, 0.0), cost=(300, 600))
    ctx = make_ctx(energy="medium", start_min=960, available=300)
    prefs = make_prefs(energy="medium", start_min=960, available=300, budget=2500, constraints=[],
                       interests=["food", "live music"], soft_preferences=["order: dinner > live music"])
    draft, report = plan_and_validate([cafe, room, resto], ctx, prefs)
    assert draft is not None and report is not None and report.ok
    notes = " ".join(s.tradeoff or "" for s in draft.stops)
    assert "dinner comes later than you asked" in notes.lower()
    assert all("dinner next, as you asked" not in s.why and "dinner first" not in s.why for s in draft.stops)


def test_no_food_stop_under_a_diet_is_explained():
    nonjain = make_candidate("fx:r", "restaurant", diet={"vegetarian": Evidence.CONFIRMED})
    park = make_candidate("fx:p", "park", offset_km=(0.3, 0.0))
    gallery = make_candidate("fx:g", "art_gallery", offset_km=(0.6, 0.0))
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=["jain food only"], interests=["food", "walks", "art"])
    draft, report = plan_and_validate([nonjain, park, gallery], ctx, prefs)
    assert draft is not None and report is not None and report.ok
    notes = [o.reason for o in draft.omitted if o.ref == "food stop"]
    assert notes and "Jain" in notes[0]
    assert "food stop" not in draft.summary_note


def test_omission_reasons_are_not_repeated():
    closed = [make_candidate(f"fx:c{i}", "cafe", offset_km=(0.1 * i, 0.0), hours=Hours(status="known", periods=[]))
              for i in range(3)]
    gallery = make_candidate("fx:g", "art_gallery", offset_km=(0.5, 0.0))
    draft, _ = plan_and_validate([*closed, gallery], make_ctx(energy="medium"),
                                 make_prefs(energy="medium", constraints=[], interests=["coffee", "art"]))
    assert draft is not None
    reasons = [o.reason for o in draft.omitted]
    assert len(reasons) == len(set(reasons))


def test_kids_places_are_not_adult_defaults_and_rain_goes_indoors():
    kids = make_candidate("fx:kids", "park", tags=["family", "kids"], rating=4.8)
    garden = make_candidate("fx:garden", "garden", offset_km=(0.3, 0.0), rating=4.3)
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.5, 0.0))
    draft, _ = plan_and_validate([kids, garden, cafe], make_ctx(energy="medium"),
                                 make_prefs(energy="medium", constraints=[], interests=["walks", "food"]))
    assert draft is not None and "fx:kids" not in {s.candidate_id for s in draft.stops}
    family, _ = plan_and_validate([kids, garden, cafe], make_ctx(energy="medium"),
                                  make_prefs(energy="medium", constraints=[], interests=["walks", "food"],
                                             mood="family day with the kids"))
    assert family is not None and "fx:kids" in {s.candidate_id for s in family.stops}
    museum = make_candidate("fx:museum", "museum", offset_km=(0.4, 0.2))
    rainy, _ = plan_and_validate([garden, cafe, museum], make_ctx(energy="medium", weather="rain"),
                                 make_prefs(energy="medium", constraints=[], interests=["walks", "food"]))
    assert rainy is not None and "fx:garden" not in {s.candidate_id for s in rainy.stops}


def test_planner_omissions_carry_names_and_avoid_long_waits():
    prefs = make_prefs(constraints=["vegetarian", "avoid crowded places"])
    draft, _ = plan_and_validate(WORLD, make_ctx(energy="low"), prefs)
    assert draft is not None
    ledger = make_ledger(*WORLD)
    for o in draft.omitted:
        if o.ref in ledger:
            assert o.name == ledger[o.ref].name
    # an event an hour after the window opens: the planner fills the gap rather than waiting
    gig = make_candidate("ev:gig", "event", offset_km=(0.3, 0.0), event_start_min=960, event_end_min=1080,
                         tags=["music"])
    cafe = make_candidate("fx:cafe", "cafe", cost=(100, 300))
    ctx = make_ctx(energy="medium", available=300)
    _, report = plan_and_validate([gig, cafe], ctx, make_prefs(energy="medium", available=300, constraints=[],
                                                               interests=["music", "food"]))
    assert report is not None and report.ok
    assert "IDLE_WAIT" not in {v.code for v in report.violations}


def test_templates_never_mention_distances():
    from app.domain.validator import numeric_claims

    cases = [
        (make_ctx(energy="low"), make_prefs(constraints=["vegetarian", "avoid crowded places"])),
        (make_ctx(energy="high", start_min=600, available=540), make_prefs(energy="high", start_min=600,
                                                                           available=540, budget=4000)),
        (make_ctx(energy="medium", weather="rain"), make_prefs(energy="medium", interests=["walks"], constraints=[])),
    ]
    for ctx, prefs in cases:
        draft, _ = plan_and_validate(WORLD, ctx, prefs)
        assert draft is not None
        texts = [s.why for s in draft.stops] + [s.tradeoff or "" for s in draft.stops] + [draft.summary_note]
        texts += [o.reason for o in draft.omitted]
        for text in texts:
            assert "distance" not in numeric_claims(text), text
            for word in ("km", "kilomet", "metre", "meter", "mile"):
                assert word not in text.lower(), text
