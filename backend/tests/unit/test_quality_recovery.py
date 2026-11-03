"""Synthetic reproductions of underfilled Mumbai, tour-business Delhi and preference loss."""
from __future__ import annotations

from datetime import UTC, datetime

from app.domain.evidence import candidate_allowed
from app.domain.parser_rules import merge_parse, parse_rules
from app.domain.quality import improve_underfilled
from app.domain.schemas import Constraint, Evidence, PlanRequest
from app.domain.travel import HeuristicTravel
from app.domain.validator import validate_plan
from app.providers.google_places import place_to_candidate
from tests.factories import make_candidate, make_ctx, make_draft, make_ledger, make_prefs


def test_short_expensive_cafe_is_replaced_by_fuller_mixed_interest_plan():
    prefs = make_prefs(interests=["food", "coffee", "museums"], soft_preferences=["prefer nearby stops"])
    expensive = make_candidate("fx:expensive", "cafe", cost=(600, 1600))
    cheap = make_candidate("fx:cheap", "cafe", cost=(200, 400), offset_km=(0.8, 0))
    museum = make_candidate("fx:museum", "museum", cost=(100, 300), offset_km=(1, 0))
    ledger = make_ledger(expensive, cheap, museum)
    original = make_draft((expensive.id, 63))
    improved = improve_underfilled(original, ledger, make_ctx(), prefs, [], HeuristicTravel())
    assert improved is not original
    ids = {s.candidate_id for s in improved.stops}
    assert cheap.id in ids and museum.id in ids and expensive.id not in ids
    report = validate_plan(improved, ledger, make_ctx(), prefs, [], HeuristicTravel())
    assert report.ok and report.plan
    assert report.plan.totals.elapsed_min >= 144
    assert report.plan.totals.worst_case <= 2000


def test_no_feasible_improvement_keeps_valid_original_not_invented_stops():
    a = make_candidate("fx:only", "cafe", cost=(600, 1600))
    draft = make_draft((a.id, 63))
    assert improve_underfilled(draft, make_ledger(a), make_ctx(), make_prefs(), [], HeuristicTravel()) is draft


def test_quality_adjustment_does_not_weaken_diet_or_pad_a_single_stop():
    a = make_candidate("fx:expensive", "cafe", cost=(600, 1600))
    cheap = make_candidate("fx:cheap", "cafe", cost=(100, 200), diet={"vegetarian": Evidence.NO})
    museum = make_candidate("fx:museum", "museum", cost=(100, 300))
    con = Constraint(kind="diet", value="vegetarian")
    prefs = make_prefs(interests=["coffee", "museums"], hard_constraints=[con])
    draft = make_draft((a.id, 63))
    assert improve_underfilled(draft, make_ledger(a, cheap, museum), make_ctx(), prefs, [con], HeuristicTravel()) is draft
    prefs.soft_preferences = ["one stop only"]
    assert improve_underfilled(draft, make_ledger(a, cheap, museum), make_ctx(), prefs, [con], HeuristicTravel()) is draft


def test_tour_business_is_not_a_scheduled_historical_attraction():
    ctx = make_ctx()
    company = make_candidate("fx:tour", "heritage_walk", name="Free Walking Tours in Example City")
    assert candidate_allowed(company, [], ctx)[0] is False
    event = company.model_copy(update={"kind":"event"})
    assert candidate_allowed(event, [], ctx)[0] is True  # event/date/time validation remains separate
    payload = {"id":"synthetic-agency", "displayName":{"text":"Example Travel"},
               "primaryType":"travel_agency", "types":["travel_agency", "point_of_interest"],
               "location":{"latitude":ctx.location.lat,"longitude":ctx.location.lng}}
    assert place_to_candidate(payload, ctx, "heritage_walk") is None
    landmark = {**payload, "primaryType":"historical_landmark", "types":["historical_landmark"]}
    assert place_to_candidate(landmark, ctx, "heritage_walk") is not None


def test_explicit_food_and_optional_cafe_survive_an_incomplete_model_parse():
    request = PlanRequest(free_text="Delhi, ₹1000, 10 AM to noon. I enjoy history and vegetarian food. "
                          "Try to include a historical attraction and a café. If both won't fit, prioritise one.")
    rules = parse_rules(request, datetime(2026,11,3,tzinfo=UTC))
    llm = rules.model_copy(deep=True)
    llm.preferences.interests = ["history"]
    llm.preferences.hard_constraints.append(Constraint(kind="must_include", value="coffee"))
    outcome = merge_parse(llm, rules, request)
    assert {"history", "food", "coffee"} <= set(outcome.preferences.interests)
    assert "must_include:coffee" not in {c.key for c in outcome.preferences.hard_constraints}
    assert "diet:vegetarian" in {c.key for c in outcome.preferences.hard_constraints}


def test_explicit_mandatory_requirement_and_soft_deduplication_are_preserved():
    request = PlanRequest(free_text="Mumbai, ₹2000, 4 hours. I must include a museum. I enjoy coffee.")
    rules = parse_rules(request, datetime(2026,11,3,tzinfo=UTC))
    rules.preferences.soft_preferences = ["Prefer nearby stops"]
    llm = rules.model_copy(deep=True)
    llm.preferences.soft_preferences = ["nearby stops"]
    outcome = merge_parse(llm, rules, request)
    assert "must_include:museum" in {c.key for c in outcome.preferences.hard_constraints}
    assert outcome.preferences.soft_preferences == ["nearby stops"]


def test_partial_backup_names_missing_interests_without_inventing_a_pacing_reason():
    from app.domain.fallback import plan_deterministic
    cafe = make_candidate("fx:only-food", "cafe", cost=(100, 200))
    prefs = make_prefs(interests=["food", "art", "museums"], available=480)
    draft = plan_deterministic([cafe], make_ctx(available=480), prefs, [], HeuristicTravel())
    assert draft is not None
    assert "does not cover art and museums" in draft.summary_note
    assert "since this pace" not in draft.summary_note
    assert "no fuller itinerary was selected" in draft.summary_note
