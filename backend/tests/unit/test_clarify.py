"""Clarification policy (check_clarity) and folding answers back into the request (resume path)."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.domain.parser_rules import (
    BUDGET_TIME_OPTIONS,
    CITY_OPTIONS,
    KEEP_AS_PREFERENCE,
    SKIP_IT,
    apply_answers,
    clarification_questions,
    original_constraints,
    parse_rules,
)
from app.domain.schemas import Location, PlanRequest

NOW = datetime.fromisoformat("2026-11-05T10:00:00+05:30")
_HERE = Path(__file__).resolve()
_EVAL_PATH = next(p for p in (_HERE.parents[3] / "eval_cases.json", _HERE.parents[2] / "evals" / "cases.json")
                  if p.exists())
EVAL = json.loads(_EVAL_PATH.read_text(encoding="utf-8"))
CASES = {c["id"]: c for c in EVAL["cases"]}


def build_request(case_id: str) -> PlanRequest:
    """Apply the eval file's merge_semantics to one case."""
    case = CASES[case_id]
    if case.get("use_defaults", True) is False:
        data: dict = {}
    else:
        data = {**EVAL["defaults"], **case.get("input_overrides", {})}
        for name in case.get("remove_fields", []):
            data.pop(name, None)
    if "free_text" in case:
        data["free_text"] = case["free_text"]
    return PlanRequest.model_validate(data)


BLR = Location(id="fixture:bangalore", name="Bangalore", country_code="IN", lat=12.9716, lng=77.5946,
               timezone="Asia/Kolkata", currency="INR")
LONDON = Location(id="fixture:london", name="London", country_code="GB", lat=51.5074, lng=-0.1278,
                  timezone="Europe/London", currency="GBP")
MUMBAI = Location(id="fixture:mumbai", name="Mumbai", country_code="IN", lat=19.076, lng=72.8777,
                  timezone="Asia/Kolkata", currency="INR")
HONG_KONG = Location(id="geo:hk", name="Hong Kong", country_code="HK", lat=22.32, lng=114.17,
                     timezone="Asia/Hong_Kong")


def questions_for(request: PlanRequest, matches: list[Location] | None = None, asked_before: bool = False):
    return clarification_questions(parse_rules(request, NOW), matches, asked_before)


def ids(questions) -> list[str]:
    return [q.id for q in questions]


# ---------------------------------------------------------------------------------------------------
# Eval cases: blocking
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case_id", ["vague_free_text", "holdout_vague_short", "clarification_answered"])
def test_vague_input_asks_city_then_budget_and_time(case_id):
    qs = questions_for(build_request(case_id))
    assert 1 <= len(qs) <= 2
    assert ids(qs) == ["city", "budget_time"]
    city = qs[0]
    assert city.field == "city" and city.options == CITY_OPTIONS
    assert qs[1].options == BUDGET_TIME_OPTIONS
    assert all(len(q.question) <= 120 for q in qs)


def test_missing_city_api_asks_only_for_the_city():
    assert ids(questions_for(build_request("missing_city_api"))) == ["city"]


@pytest.mark.parametrize("matches", [[LONDON], None])
def test_currency_ambiguity_abroad_asks_gbp_or_inr(matches):
    qs = questions_for(build_request("currency_ambiguity_abroad"), matches)
    assert ids(qs) == ["currency"]
    q = qs[0]
    assert "2000" in q.question and "GBP" in q.question and "INR" in q.question
    assert q.options == ["GBP", "INR"]
    assert q.field == "currency"


def test_explicit_currency_abroad_is_not_asked():
    request = PlanRequest(city="London", budget=40, currency="GBP", available_time="4 hours")
    assert questions_for(request, [LONDON]) == []
    assert questions_for(PlanRequest(free_text="London, £40, free for 4 hours"), [LONDON]) == []


def test_country_without_default_currency_asks_which_currency():
    qs = questions_for(PlanRequest(city="Hong Kong", budget=500, available_time="3 hours"), [HONG_KONG])
    assert ids(qs) == ["currency"]
    assert qs[0].options == ["INR", "USD"]


def test_unknown_city_says_not_found():
    qs = questions_for(build_request("unknown_city"), [])
    assert ids(qs) == ["city_not_found"]
    assert "Atlantisville" in qs[0].question and "couldn't find" in qs[0].question
    assert qs[0].options == CITY_OPTIONS  # no close match: popular cities, never invented places


def test_unknown_city_offers_close_matches():
    qs = questions_for(PlanRequest(city="Banglaore", budget=1000, available_time="3 hours"), [])
    assert ids(qs) == ["city_not_found"]
    assert qs[0].options[0] == "Bangalore"


def test_contradictory_constraints_names_both_and_asks_which_to_keep():
    qs = questions_for(build_request("contradictory_constraints"), [BLR])
    assert ids(qs) == ["conflict"]
    q = qs[0]
    assert "indoors only" in q.question and "must include an outdoor walk" in q.question
    assert q.options == ["indoors only", "must include an outdoor walk"]


def test_conflict_from_one_free_text_sentence_uses_distinct_labels():
    request = PlanRequest(free_text="Bangalore, ₹1000, 3 hours. Indoors only but must include an outdoor walk.")
    qs = questions_for(request, [BLR])
    assert ids(qs) == ["conflict"]
    assert len(set(qs[0].options)) == 2


def test_unmappable_hard_constraint_is_asked_last():
    request = PlanRequest(city="Bangalore", budget=1000, available_time="3 hours",
                          constraints=["vegetarian", "no stairs please"])
    qs = questions_for(request, [BLR])
    assert ids(qs) == ["constraint"]
    assert "no stairs please" in qs[0].question
    assert qs[0].options == [KEEP_AS_PREFERENCE, SKIP_IT]


# ---------------------------------------------------------------------------------------------------
# Eval cases: not blocking
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id",
    ["assignment_example", "city_alias_bengaluru", "free_text_complete", "benign_ignore_phrase",
     "high_energy_long_day", "tight_budget_free_activities", "wheelchair_access", "date_night_no_diet",
     "impossible_budget", "late_night_start", "rain_forecast", "diet_dropped_by_parser", "xss_in_input",
     "holdout_rainy_museum", "holdout_jain_diet", "holdout_small_budget_music", "holdout_quiet_vs_live_music",
     "holdout_family_long_day", "holdout_koramangala_brunch"],
)
def test_feasible_cases_ask_nothing(case_id):
    assert questions_for(build_request(case_id), [BLR]) == []


def test_other_indian_city_asks_nothing():
    assert questions_for(build_request("other_indian_city"), [MUMBAI]) == []


@pytest.mark.parametrize("case_id", ["prompt_injection_in_mood", "holdout_injection_variant"])
def test_injection_text_is_never_asked_about(case_id):
    outcome = parse_rules(build_request(case_id), NOW)
    assert clarification_questions(outcome, [BLR], False) == []


def test_asked_before_returns_no_questions():
    for case_id in ("vague_free_text", "contradictory_constraints", "currency_ambiguity_abroad", "unknown_city"):
        assert questions_for(build_request(case_id), [], asked_before=True) == []


def test_priority_and_cap_of_two():
    request = PlanRequest(free_text="plan something fun", constraints=["indoors only", "must include an outdoor walk",
                                                                       "no stairs please"])
    qs = questions_for(request)
    assert ids(qs) == ["city", "conflict"]
    request = PlanRequest(city="London", budget=50, available_time="3 hours",
                          constraints=["no stairs please", "indoors only", "must include an outdoor walk"])
    assert ids(questions_for(request, [LONDON])) == ["conflict", "currency"]


def test_budget_time_only_asked_with_missing_city():
    assert questions_for(PlanRequest(city="Bangalore"), [BLR]) == []


# ---------------------------------------------------------------------------------------------------
# apply_answers
# ---------------------------------------------------------------------------------------------------


def test_clarification_answered_resumes_with_a_complete_request():
    case = CASES["clarification_answered"]
    request = build_request("clarification_answered")
    before = request.model_dump()
    answered = apply_answers(request, case["clarification_answers"])
    assert request.model_dump() == before  # input untouched
    outcome = parse_rules(answered, NOW)
    assert outcome.missing == []
    prefs = outcome.preferences
    assert (prefs.city, prefs.budget, prefs.available_minutes, prefs.start_min) == ("Bangalore", 1000, 180, 780)
    assert clarification_questions(outcome, [BLR], asked_before=True) == []
    assert clarification_questions(outcome, [BLR], asked_before=False) == []


@pytest.mark.parametrize(
    ("answer", "currency"),
    [("GBP", "GBP"), ("INR", "INR"), ("₹", "INR"), ("£", "GBP"), ("pounds", "GBP"), ("rupees", "INR"),
     ("Indian rupees (INR)", "INR"), ("usd", "USD"), ("local currency", "GBP"), ("no idea", None)],
)
def test_currency_answers(answer, currency):
    request = build_request("currency_ambiguity_abroad")
    answered = apply_answers(request, {"currency": answer})
    assert answered.currency == currency
    outcome = parse_rules(answered, NOW)
    assert outcome.budget_currency_explicit is (currency is not None)
    assert outcome.preferences.budget == 2000
    if currency:
        assert clarification_questions(outcome, [LONDON], False) == []


@pytest.mark.parametrize(
    ("answer", "kept"),
    [("indoors only", "setting:indoors_only"), ("must include an outdoor walk", "must_include:outdoor_walk"),
     ("Indoors only", "setting:indoors_only"), ("the walk please", "must_include:outdoor_walk")],
)
def test_conflict_answer_drops_the_other_constraint(answer, kept):
    request = build_request("contradictory_constraints")
    answered = apply_answers(request, {"conflict": answer})
    hard = [c.key for c in original_constraints(answered)]
    assert kept in hard
    assert len(hard) == 1
    assert questions_for(answered, [BLR]) == []


def test_conflict_answer_neither_and_unrecognised():
    request = build_request("contradictory_constraints")
    assert apply_answers(request, {"conflict": "neither"}).constraints == []
    assert apply_answers(request, {"conflict": "hmm"}).constraints == request.constraints


def test_conflict_answer_edits_free_text_clause_only():
    request = PlanRequest(free_text="I'm in Bangalore, ₹1000, 3 hours. Indoors only but must include an outdoor walk.")
    answered = apply_answers(request, {"conflict": "Indoors only"})
    outcome = parse_rules(answered, NOW)
    assert [c.key for c in outcome.preferences.hard_constraints] == ["setting:indoors_only"]
    assert (outcome.preferences.city, outcome.preferences.budget) == ("Bangalore", 1000)
    assert outcome.conflicts == []


@pytest.mark.parametrize(
    ("answer", "budget", "currency", "minutes"),
    [("3 hours, ₹1000", 1000, "INR", 180), ("4 hours, ₹2000", 2000, "INR", 240),
     ("Whole day, ₹3000", 3000, "INR", 600), ("2 hours, around 800", 800, None, 120)],
)
def test_budget_time_answers(answer, budget, currency, minutes):
    request = build_request("vague_free_text")
    answered = apply_answers(request, {"city": "Bangalore", "budget_time": answer})
    outcome = parse_rules(answered, NOW)
    assert outcome.missing == []
    assert (outcome.preferences.budget, outcome.preferences.currency) == (budget, currency)
    assert outcome.preferences.available_minutes == minutes
    if minutes > 480:
        assert outcome.preferences.start_min == 600


def test_constraint_answers_replace_skip_or_keep():
    request = PlanRequest(city="Bangalore", budget=1000, available_time="3 hours",
                          constraints=["vegetarian", "no stairs please"])
    replaced = apply_answers(request, {"constraint": "wheelchair accessible"})
    assert replaced.constraints == ["vegetarian", "wheelchair accessible"]
    assert [c.key for c in parse_rules(replaced, NOW).preferences.hard_constraints] == [
        "diet:vegetarian", "access:wheelchair"]
    assert apply_answers(request, {"constraint": SKIP_IT}).constraints == ["vegetarian"]
    kept = apply_answers(request, {"constraint": KEEP_AS_PREFERENCE})
    assert kept.constraints == request.constraints
    assert questions_for(kept, [BLR], asked_before=True) == []


def test_constraint_answer_for_free_text_clause():
    request = PlanRequest(free_text="Bangalore, ₹1000, 3 hours. Vegetarian and no spicy food")
    answered = apply_answers(request, {"constraint": "vegan"})
    outcome = parse_rules(answered, NOW)
    assert outcome.unmapped_constraints == []
    assert [c.key for c in outcome.preferences.hard_constraints] == ["diet:vegan"]


def test_city_answers_and_ignored_keys():
    request = build_request("unknown_city")
    answered = apply_answers(request, {"city_not_found": "Bengaluru", "unknown_key": "x", "budget": "no idea"})
    assert answered.city == "Bengaluru"
    assert answered.budget == request.budget
    assert parse_rules(answered, NOW).preferences.city == "Bangalore"
    assert apply_answers(request, {}) == request


def test_field_answers():
    request = PlanRequest(free_text="something fun")
    answered = apply_answers(request, {"city": "Mumbai", "start_time": "5pm", "neighbourhood": "Bandra",
                                       "available_time": "2 hours", "budget": "₹900"})
    prefs = parse_rules(answered, NOW).preferences
    assert (prefs.city, prefs.neighbourhood, prefs.start_min, prefs.available_minutes) == ("Mumbai", "Bandra", 1020, 120)
    assert (prefs.budget, prefs.currency) == (900, "INR")


# ---------------------------------------------------------------------------------------------------
# Verification fixes
# ---------------------------------------------------------------------------------------------------


def test_unread_amount_with_a_city_is_an_assumption_not_a_question():
    request = PlanRequest(free_text="Bangalore, 600 for us, 4 hours")
    assert questions_for(request, [BLR]) == []


def test_unread_amount_without_a_city_asks_budget_and_time():
    assert ids(questions_for(PlanRequest(free_text="600 for us, something fun"))) == ["city", "budget_time"]


def test_london_answered_inr_is_converted_not_mixed():
    from app.domain.parser_rules import apply_defaults

    request = build_request("currency_ambiguity_abroad")
    answered = apply_answers(request, {"currency": "INR"})
    outcome = parse_rules(answered, NOW)
    assert clarification_questions(outcome, [LONDON], asked_before=True) == []
    prefs = apply_defaults(outcome, LONDON)
    assert prefs.currency == "GBP" and prefs.budget == 15


def test_negated_diet_never_blocks():
    assert questions_for(PlanRequest(free_text="Bangalore, I'm not vegetarian, 2000 rupees, 4 hours"), [BLR]) == []


def test_apply_answers_preserves_relaxed_constraints():
    request = PlanRequest(free_text="vegan, wheelchair access, something fun", relaxed_constraints=["access:wheelchair"])
    answered = apply_answers(request, {"city": "Bangalore", "budget_time": "3 hours, ₹1000"})
    assert answered.relaxed_constraints == ["access:wheelchair"]
    assert "access:wheelchair" not in {c.key for c in original_constraints(answered)}
