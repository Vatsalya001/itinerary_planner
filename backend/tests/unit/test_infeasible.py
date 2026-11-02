import pytest

from app.domain.constraints import map_constraint_text
from app.domain.fallback import plan_deterministic
from app.domain.infeasible import constraint_label, explain_infeasible
from app.domain.schemas import Constraint, Evidence, Hours
from app.domain.travel import HeuristicTravel
from tests.factories import (
    bangalore_like_world,
    make_candidate,
    make_ctx,
    make_prefs,
    synthetic_candidates,
)

travel = HeuristicTravel()
WORLD = bangalore_like_world()


def test_impossible_budget_offers_cheapest_budget():
    ctx = make_ctx(energy="low")
    prefs = make_prefs(budget=1, constraints=["vegetarian", "wheelchair accessible", "include a sit-down meal"])
    assert [c.key for c in prefs.hard_constraints] == ["diet:vegetarian", "access:wheelchair",
                                                       "must_include:sit_down_meal"]
    report = explain_infeasible(WORLD, ctx, prefs, prefs.hard_constraints, travel)
    assert report.cheapest_budget is not None and report.cheapest_budget > 1
    assert "budget" in report.binding_constraints
    budget_fix = [r for r in report.relaxations if "budget" in r.patch]
    assert budget_fix
    assert budget_fix[0].label == f"Raise budget to ₹{report.cheapest_budget}"
    assert budget_fix[0].patch == {"budget": report.cheapest_budget}
    assert str(report.cheapest_budget) in report.reason
    # applying the one-tap fix yields a valid plan with every constraint still enforced
    fixed = prefs.model_copy(update={"budget": report.cheapest_budget})
    assert plan_deterministic(WORLD, ctx, fixed, fixed.hard_constraints, travel) is not None
    # one rupee less is still infeasible: it really is the cheapest found
    tighter = prefs.model_copy(update={"budget": report.cheapest_budget - 1})
    assert plan_deterministic(WORLD, ctx, tighter, tighter.hard_constraints, travel) is None


def test_drop_constraint_relaxation():
    cands = [make_candidate(f"fx:c{i}", key, offset_km=(0.3 * i, 0.0), wheelchair=Evidence.UNKNOWN)
             for i, key in enumerate(["cafe", "bookstore", "restaurant", "art_gallery"])]
    ctx = make_ctx(energy="low")
    prefs = make_prefs(constraints=["wheelchair accessible", "vegetarian"])
    report = explain_infeasible(cands, ctx, prefs, prefs.hard_constraints, travel)
    labels = [r.label for r in report.relaxations]
    assert "Drop 'wheelchair accessible'" in labels
    drop = next(r for r in report.relaxations if r.label == "Drop 'wheelchair accessible'")
    assert drop.patch == {"relaxed_constraints": ["access:wheelchair"]}
    assert "access:wheelchair" in report.binding_constraints
    assert "wheelchair" in report.reason.lower()
    remaining = [c for c in prefs.hard_constraints if c.key not in drop.patch["relaxed_constraints"]]
    relaxed = prefs.model_copy(update={"hard_constraints": remaining})
    assert plan_deterministic(cands, ctx, relaxed, remaining, travel) is not None


def test_drop_patch_names_only_the_dropped_key():
    shared = map_constraint_text("vegetarian, wheelchair accessible")
    assert {c.key for c in shared} == {"diet:vegetarian", "access:wheelchair"}
    cands = [make_candidate(f"fx:c{i}", key, offset_km=(0.3 * i, 0.0), wheelchair=Evidence.UNKNOWN)
             for i, key in enumerate(["cafe", "bookstore", "restaurant"])]
    prefs = make_prefs(constraints=shared)
    report = explain_infeasible(cands, make_ctx(), prefs, prefs.hard_constraints, travel)
    drop = next(r for r in report.relaxations if "wheelchair" in r.label)
    assert drop.patch == {"relaxed_constraints": ["access:wheelchair"]}
    from app.domain.schemas import PlanRequest

    PlanRequest(free_text="vegetarian, wheelchair accessible", **drop.patch)  # a valid request patch


def test_time_relaxation():
    ctx = make_ctx(energy="medium", available=30)
    prefs = make_prefs(energy="medium", available=30, constraints=["must_include:sit_down_meal"])
    report = explain_infeasible(WORLD, ctx, prefs, prefs.hard_constraints, travel)
    assert report.min_minutes is not None and report.min_minutes > 30
    assert "time" in report.binding_constraints
    fix = next(r for r in report.relaxations if "available_time" in r.patch)
    assert fix.patch == {"available_time": report.min_minutes}
    longer_ctx = make_ctx(energy="medium", available=report.min_minutes)
    longer = prefs.model_copy(update={"available_minutes": report.min_minutes})
    assert plan_deterministic(WORLD, longer_ctx, longer, longer.hard_constraints, travel) is not None


def test_late_start_suggests_default_start():
    ctx = make_ctx(start_min=23 * 60, available=180)
    prefs = make_prefs(start_min=23 * 60, available=180, constraints=[])
    report = explain_infeasible(WORLD, ctx, prefs, [], travel)
    assert report.reason
    assert any(r.patch == {"start_time": "15:00"} for r in report.relaxations)


def test_no_candidates():
    report = explain_infeasible([], make_ctx(), make_prefs(), [], travel)
    assert "No places" in report.reason
    assert report.relaxations == []


def test_feasible_input_needs_nothing():
    prefs = make_prefs(constraints=["vegetarian"])
    report = explain_infeasible(WORLD, make_ctx(), prefs, prefs.hard_constraints, travel)
    assert report.relaxations == []
    assert report.cheapest_budget is not None


@pytest.mark.parametrize("key", [
    "diet:vegetarian", "diet:vegan", "diet:jain", "alcohol:avoid", "access:wheelchair", "crowd:avoid",
    "setting:indoors_only", "setting:outdoors_only", "must_include:sit_down_meal", "must_include:outdoor_walk",
    "must_include:live_music", "must_include:museum", "must_avoid:mall", "must_avoid:market",
])
def test_constraint_labels_round_trip_through_rules_parser(key):
    kind, value = key.split(":")
    label = constraint_label(Constraint(kind=kind, value=value))
    assert key in {c.key for c in map_constraint_text(label)}


def test_runs_quickly_on_sixty_candidates():
    import time

    cands = synthetic_candidates(60)
    prefs = make_prefs(budget=1, constraints=["vegetarian", "include a sit-down meal"])
    t0 = time.perf_counter()
    report = explain_infeasible(cands, make_ctx(), prefs, prefs.hard_constraints, travel)
    assert time.perf_counter() - t0 < 5.0
    assert report.cheapest_budget is not None and report.cheapest_budget > 1


def test_pacing_rule_is_named_when_it_binds():
    # outdoors only, low energy, and a seated outdoor option exists but is closed: the seated rule binds
    walks = [make_candidate(f"fx:w{i}", key, offset_km=(0.4 * i, 0.0)) for i, key in enumerate(["park", "garden"])]
    terrace = make_candidate("fx:terrace", "street_food", seated=True, offset_km=(0.2, 0.2),
                             hours=Hours(status="known", periods=[(1200, 1320)]))
    ctx = make_ctx(energy="low", start_min=600)
    prefs = make_prefs(energy="low", start_min=600, interests=["walks"], constraints=["outdoors only"])
    assert plan_deterministic([*walks, terrace], ctx, prefs, prefs.hard_constraints, travel) is None
    report = explain_infeasible([*walks, terrace], ctx, prefs, prefs.hard_constraints, travel)
    assert "seated" in report.reason
    assert "pacing:seated" in report.binding_constraints
    assert "Even with an unlimited budget" not in report.reason
    assert any(r.label == "Plan at a moderate pace" for r in report.relaxations)


def test_meal_timing_is_named_and_access_is_not_dropped():
    # only a wheelchair-accessible restaurant fits; at 11:00 lunch can't start yet
    resto = make_candidate("fx:resto", "restaurant", cost=(200, 400))
    others = [make_candidate(f"fx:o{i}", key, offset_km=(0.3 * i, 0.0), wheelchair=Evidence.UNKNOWN)
              for i, key in enumerate(["cafe", "bookstore"], start=1)]
    ctx = make_ctx(energy="low", start_min=660, available=60)
    prefs = make_prefs(start_min=660, available=60, budget=800, constraints=["wheelchair accessible"])
    assert plan_deterministic([resto, *others], ctx, prefs, prefs.hard_constraints, travel) is None
    report = explain_infeasible([resto, *others], ctx, prefs, prefs.hard_constraints, travel)
    assert "meal_timing" in report.binding_constraints
    labels = [r.label for r in report.relaxations]
    assert any(label.startswith("Start at") for label in labels), labels
    assert not any("wheelchair" in label.lower() for label in labels), labels
    assert "meal" in report.reason.lower()


def test_empty_pool_with_a_small_budget_offers_a_budget_raise():
    from tests.factories import LONDON

    ctx = make_ctx(location=LONDON)
    prefs = make_prefs(budget=15, currency="GBP", city="London")
    report = explain_infeasible([], ctx, prefs, [], travel)
    assert "no places or events were found" not in report.reason.lower()
    assert report.relaxations and report.relaxations[0].patch == {"budget": 40, "currency": "GBP"}
    plain = explain_infeasible([], ctx, make_prefs(budget=None, currency="GBP", city="London"), [], travel)
    assert "No places" in plain.reason
