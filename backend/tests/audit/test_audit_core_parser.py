"""Independent audit (core): rules parser, time/money extraction and constraint mapping.

Every test marked ``xfail(strict=True)`` reproduces a confirmed bug (see docs/verification/AUDIT_CORE.md, AUD-<n>).
When a bug is fixed the test XPASSes, which fails the run under strict mode: remove the marker then.
Offline only: fixture providers, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.baseline import run_baseline
from app.deps import build_deps, fixture_settings
from app.domain.constraints import map_constraint_text
from app.domain.parser_rules import (
    apply_defaults,
    clarification_questions,
    merge_parse,
    parse_rules,
)
from app.domain.schemas import Constraint, Evidence, Location, ParseOutcome, PlanRequest, Preferences
from app.domain.timeutil import parse_time_range

NOW = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)  # Thursday 10:00 IST, as in fixture_settings()
BLR = Location(id="fixture:bangalore", name="Bengaluru", country_code="IN", lat=12.9716, lng=77.5946,
               timezone="Asia/Kolkata", currency="INR")


def prefs_for(**request: object) -> Preferences:
    req = PlanRequest(**request)  # type: ignore[arg-type,unused-ignore]
    return apply_defaults(merge_parse(None, parse_rules(req, NOW), req), BLR)


def outcome_for(**request: object) -> ParseOutcome:
    req = PlanRequest(**request)  # type: ignore[arg-type,unused-ignore]
    return merge_parse(None, parse_rules(req, NOW), req)


# ---------------------------------------------------------------------------------------------------
# AUD-1 (P0): "I don't eat meat" is read as "I eat meat" -> "No diet restriction", non-veg stop planned
# ---------------------------------------------------------------------------------------------------

MEAT_NEGATIONS = ["I don't eat meat", "I do not eat meat", "I dont eat meat", "we never eat meat"]


@pytest.mark.parametrize("text", MEAT_NEGATIONS)
def test_aud1_dont_eat_meat_means_vegetarian(text: str) -> None:
    keys = {c.key for c in outcome_for(city="Bangalore", budget=2000, constraints=[text]).preferences.hard_constraints}
    assert "diet:vegetarian" in keys


def test_aud1_dont_eat_meat_is_not_reported_as_no_restriction() -> None:
    soft = outcome_for(city="Bangalore", budget=2000, constraints=["I don't eat meat"]).preferences.soft_preferences
    assert "No diet restriction" not in soft


def test_aud1_llm_vegetarian_constraint_survives_merge() -> None:
    req = PlanRequest(city="Bangalore", budget=2000, available_time=240, constraints=["I don't eat meat"])
    llm = ParseOutcome(preferences=Preferences(city="Bangalore", budget=2000, hard_constraints=[
        Constraint(kind="diet", value="vegetarian", source_text="I don't eat meat")]), source="llm")
    merged = merge_parse(llm, parse_rules(req, NOW), req)
    assert "diet:vegetarian" in {c.key for c in merged.preferences.hard_constraints}


async def test_aud1_end_to_end_no_non_veg_stop() -> None:
    deps = build_deps(fixture_settings())
    req = PlanRequest(city="Bangalore", budget=3000, available_time=240, start_time="19:00", interests=["food", "music"],
                      constraints=["I don't eat meat"])
    result = await run_baseline(req, deps)
    assert result.plan is not None
    non_veg = [s.candidate.name for s in result.plan.stops if s.candidate.diet.get("vegetarian") == Evidence.NO]
    assert non_veg == [], f"non-vegetarian stops planned for a user who doesn't eat meat: {non_veg}"


# ---------------------------------------------------------------------------------------------------
# AUD-2 (P0): "2pm to 6" / "4.30pm to 8" / "11am to 2": the bare end is read as the next morning
# ---------------------------------------------------------------------------------------------------

RANGE_CASES = [
    ("free 2pm to 6", 840, 1080),
    ("from 4:30pm to 8", 990, 1200),
    ("from 6 pm till 10", 1080, 1320),
    ("11am to 2", 660, 840),
    ("12pm to 3", 720, 900),
]


@pytest.mark.parametrize(("text", "start", "end"), RANGE_CASES)
def test_aud2_bare_end_inherits_start_meridiem(text: str, start: int, end: int) -> None:
    assert parse_time_range(text) == (start, end)


async def test_aud2_plan_ends_by_the_stated_end_time() -> None:
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(free_text="I'm free from 4.30pm to 8 in Bangalore with 1.5k"), deps)
    assert result.preferences is not None
    assert result.preferences.available_minutes == 210
    assert result.plan is not None and result.plan.end_min <= 20 * 60


# ---------------------------------------------------------------------------------------------------
# AUD-3 (P0): "between 4 and 8 pm" starts the plan at 8 PM; "between 4pm and 8pm" ignores both times
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Bangalore, between 4 and 8 pm, budget 1000", "Bangalore, between 4pm and 8pm, budget 1000"])
def test_aud3_between_range(text: str) -> None:
    p = prefs_for(free_text=text)
    assert (p.start_min, p.available_minutes) == (960, 240)


async def test_aud3_plan_inside_between_window() -> None:
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(free_text="Bangalore, between 4 and 8 pm, budget 1000"), deps)
    assert result.plan is not None
    assert result.plan.start_min >= 16 * 60 and result.plan.end_min <= 20 * 60


# ---------------------------------------------------------------------------------------------------
# AUD-15 (P2): structured available_time text is not clamped to 30..720 (schema docstring promises it)
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["15 hours", "5 min", "0.2 hours"])
def test_aud15_text_available_time_is_clamped(text: str) -> None:
    p = prefs_for(city="Bangalore", budget=3000, available_time=text, start_time="10:00")
    assert 30 <= p.available_minutes <= 720


# ---------------------------------------------------------------------------------------------------
# AUD-16 (P2): "an hour and a half" -> 60 min; "2 and a half hours" / "1 and a half hours" -> not read
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "minutes"), [("an hour and a half", 90), ("2 and a half hours", 150),
                                               ("1 and a half hours", 90)])
def test_aud16_and_a_half_durations(text: str, minutes: int) -> None:
    p = prefs_for(free_text=f"Bangalore, {text}, ₹1000")
    assert p.available_minutes == minutes


# ---------------------------------------------------------------------------------------------------
# AUD-17 (P2): an amount the parser can't attribute is replaced by a HIGHER default budget
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "said"), [("Bangalore, 3 hours, 1200/-", 1200), ("Bangalore, 3:30 to 6:30, 1000", 1000),
                                            ("Bangalore, 2 hrs and 30 mins, 500", 500)])
def test_aud17_budget_never_above_the_users_number(text: str, said: int) -> None:
    p = prefs_for(free_text=text)
    assert p.budget is not None and p.budget <= said


# ---------------------------------------------------------------------------------------------------
# AUD-18 (P2): "no problem with X" / "both fine" phrasings map to the opposite constraint
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "key"), [("no problem with crowds", "crowd:avoid"),
                                           ("no issues with drinks", "alcohol:avoid"),
                                           ("veg and non-veg both fine", "diet:vegetarian")])
def test_aud18_permissive_phrasing_adds_no_constraint(text: str, key: str) -> None:
    assert key not in {c.key for c in map_constraint_text(text)}


def test_aud18_negated_access_need_is_not_a_question() -> None:
    outcome = outcome_for(city="Bangalore", budget=1000, available_time=240, constraints=["no wheelchair needed"])
    assert clarification_questions(outcome, None, False) == []


# ---------------------------------------------------------------------------------------------------
# AUD-21 (P2, documented limitation): "from 5 to 9" is ignored and a 15:00 start is assumed
# ---------------------------------------------------------------------------------------------------

def test_aud21_from_bare_range_is_read_as_afternoon() -> None:
    p = prefs_for(free_text="Bangalore, free from 5 to 9, ₹2000")
    assert (p.start_min, p.available_minutes) == (17 * 60, 240)
