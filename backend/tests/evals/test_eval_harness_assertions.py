"""The independent assertions pass on a clean run and catch injected faults (offline, scripted fake model)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.schemas import TraceEvent
from app.providers.fixture import load_world
from evals.assertions import (
    CASE_CHECKS,
    Check,
    Observation,
    evaluate,
    free_text_budget,
    free_text_window,
    hard_pass,
    names_pattern,
    prose_notes,
    prose_problems,
)
from tests.evals.helpers import CASES, SPEC, observe


def failed(checks: list[Check]) -> dict[str, str]:
    return {c.name: c.detail for c in checks if c.kind == "code" and c.passed is False}


def mutated(obs: Observation) -> Observation:
    """A deep copy whose final result can be edited without touching the original."""
    clone = copy.copy(obs)
    clone.phases = [copy.copy(p) for p in obs.phases]
    last = clone.phases[-1]
    assert last.result is not None
    last.result = last.result.model_copy(deep=True)
    last.events = list(last.events)
    clone.data = copy.deepcopy(obs.data)
    clone.call_log = copy.deepcopy(obs.call_log)
    clone.plan_checks = []
    return clone


@pytest.fixture
async def clean() -> Observation:
    return await observe("assignment_example")


async def test_clean_run_passes_every_code_assertion(clean: Observation) -> None:
    checks = evaluate(clean)
    assert failed(checks) == {}
    assert hard_pass(checks)
    names = {c.name for c in checks}
    assert {"status.expected", "trace.integrity", "trace.no_secrets", "plan.grounded_ids", "plan.cost_within_budget",
            "plan.time_within_window", "plan.opening_hours", "plan.hard_constraints", "plan.pacing",
            "plan.prose_no_numbers", "plan.why_and_source_badge"} <= names
    assert sum(1 for c in checks if c.name.startswith("case.")) == len(CASES["assignment_example"].assertions)


async def test_over_budget_is_caught(clean: Observation) -> None:
    obs = mutated(clean)
    obs.data["budget"] = 100  # the request now says 100; the plan was built for 2000
    assert "plan.cost_within_budget" in failed(evaluate(obs))


async def test_duplicate_stop_is_caught(clean: Observation) -> None:
    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None
    plan.stops.append(plan.stops[0].model_copy(deep=True))
    assert "plan.no_duplicates" in failed(evaluate(obs))


async def test_unknown_and_ungrounded_ids_are_caught(clean: Observation) -> None:
    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None and obs.call_log is not None
    obs.call_log.ids.discard(plan.stops[-1].candidate.id)  # never returned by a provider in this run
    detail = failed(evaluate(obs))["plan.grounded_ids"]
    assert "no provider returned this ID" in detail

    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None
    plan.stops[0].candidate.id = "fx:blr-invented-place"
    assert "not a known fixture" in failed(evaluate(obs))["plan.grounded_ids"]


async def test_non_vegetarian_food_stop_is_caught(clean: Observation) -> None:
    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None and obs.call_log is not None
    kebab = next(c for c in load_world().candidates["fixture:bangalore"] if c.id == "fx:blr-residency-kebab-house")
    food = next(s for s in plan.stops if s.candidate.category in {"restaurant", "cafe"})
    food.candidate = kebab.model_copy(deep=True)
    obs.call_log.ids.add(kebab.id)
    fails = failed(evaluate(obs))
    assert "without confirmed vegetarian evidence" in fails["plan.hard_constraints"]
    assert "case.3.food_stops_carry_positive_vegetarian_evidence" in fails


async def test_closed_hours_and_prose_numbers_are_caught(clean: Observation) -> None:
    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None
    stop = plan.stops[0]
    stop.start_min, stop.end_min = 1500, 1500 + stop.duration_min  # 01:00 the next morning
    stop.why = "Only ₹200 and 30 minutes from the park, open till 9pm"
    fails = failed(evaluate(obs))
    assert "plan.opening_hours" in fails or "plan.time_within_window" in fails
    assert "plan.prose_no_numbers" in fails


async def test_distance_in_a_tradeoff_is_caught_but_a_place_name_is_not(clean: Observation) -> None:
    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None
    plan.stops[0].tradeoff = "Only 2.2 km from " + plan.stops[-1].candidate.name
    assert "distance" in failed(evaluate(obs))["plan.prose_no_numbers"]
    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None
    plan.stops[0].tradeoff = "Close to " + plan.stops[-1].candidate.name
    assert "plan.prose_no_numbers" not in failed(evaluate(obs))


async def test_secret_leak_and_broken_trace_are_caught(clean: Observation) -> None:
    obs = mutated(clean)
    events = obs.phases[-1].events
    leak = TraceEvent(type="tool_completed", run_id=events[0].run_id, seq=events[-1].seq + 1, ts=datetime.now(UTC),
                      summary="key sk-ant-SENTINEL-maps-0001")
    obs.phases[-1].events = [*events[:-2], leak, *events[-2:]]
    fails = failed(evaluate(obs))
    assert "trace.no_secrets" in fails and "trace.integrity" in fails  # seq out of order, unmatched tool event

    obs = mutated(clean)
    obs.phases[-1].events = [e for e in obs.phases[-1].events if not (e.type == "node_completed" and e.node == "finalize")]
    assert "trace.integrity" in failed(evaluate(obs))


async def test_wrong_status_fails_and_baseline_equivalence_holds(clean: Observation) -> None:
    obs = mutated(clean)
    obs.phases[-1].result = obs.result.model_copy(update={"status": "fallback", "mode": "deterministic"})  # type: ignore[union-attr]
    fails = failed(evaluate(obs))
    assert "status.expected" in fails and "case.1.mode_is_agent_not_deterministic_outside_the_baseline_row" in fails
    # the no-LLM baseline never reports "fallback": its deterministic "complete" satisfies a fallback expectation
    from evals.assertions import status_ok

    timeout_case = CASES["model_timeout"]
    obs.case, obs.kind = timeout_case, "baseline"
    obs.phases[-1].result = obs.result.model_copy(update={"status": "complete", "mode": "deterministic"})  # type: ignore[union-attr]
    assert status_ok(obs)


async def test_rain_without_plan_b_is_caught() -> None:
    obs = await observe("rain_forecast")
    assert failed(evaluate(obs)) == {}
    bad = mutated(obs)
    plan = bad.result.plan  # type: ignore[union-attr]
    assert plan is not None and bad.call_log is not None
    park = next(c for c in load_world().candidates["fixture:bangalore"] if c.id == "fx:blr-cubbon-park")
    stop = plan.stops[-1]
    stop.candidate, stop.plan_b = park.model_copy(deep=True), None  # an outdoor stop in rain, no Plan B
    bad.call_log.ids.add(park.id)
    fails = failed(evaluate(bad))
    assert "outdoor in rain without a Plan B" in fails["plan.rain_plan_b"]
    assert "case.2.every_outdoor_stop_has_an_indoor_plan_b" in fails


async def test_clarification_case_assertions() -> None:
    obs = await observe("vague_free_text")
    checks = evaluate(obs)
    assert failed(checks) == {}
    assert obs.extra.get("resumable") is True
    plan_checks = [c for c in checks if c.name.startswith("plan.")]
    assert plan_checks == []  # no plan, so no plan checks are applied
    answered = await observe("clarification_answered")
    assert len(answered.phases) == 2 and failed(evaluate(answered)) == {}


async def test_parse_expect_mismatch_is_caught() -> None:
    obs = await observe("free_text_complete")
    assert failed(evaluate(obs)) == {}
    bad = mutated(obs)
    prefs = bad.result.preferences  # type: ignore[union-attr]
    assert prefs is not None
    prefs.budget = 1400
    fails = failed(evaluate(bad))
    assert "budget: got 1400" in fails["case.parse_expect"]
    assert "plan.cost_within_budget" not in fails  # the request text still says 1500, which the plan meets


def test_every_case_assertion_is_implemented() -> None:
    for case in SPEC["cases"]:
        mapped = [text for text, _, _ in CASE_CHECKS.get(case["id"], [])]
        assert mapped == case["assertions"], case["id"]


def test_judge_kind_assertions_never_block_hard_pass() -> None:
    checks = [Check("a", True), Check("b", False, kind="judge"), Check("c", None)]
    assert hard_pass(checks)
    assert not hard_pass([*checks, Check("d", False)])
    raw: list[dict[str, Any]] = [{"kind": "judge", "passed": False}, {"kind": "code", "passed": None}]
    assert hard_pass(raw)


def test_free_text_readers() -> None:
    assert free_text_window("Got about 1500 rupees and I'm free 4 to 8pm.") == (960, 240)
    assert free_text_window("late brunch then something artsy, 11am to 3pm, feeling okay") == (660, 240)
    assert free_text_window("no times here") is None
    assert free_text_budget("Got about 1500 rupees") == 1500
    assert free_text_budget("1200 per person, 11am to 3pm") == 1200
    assert free_text_budget("plan something fun") is None


def test_prose_problems() -> None:
    assert prose_problems("A calm cafe with good filter coffee") == []
    assert set(prose_problems("Costs ₹300, about 45 minutes, until 6pm, opens 10:30")) >= {
        "currency symbol", "number with unit", "clock time"}
    assert prose_problems("Spend a couple of hours here") == ["word duration"]
    # distances are numeric claims too (eval_cases.json all_plan_assertions); percentages are only noted
    for text in ("A short hop (2.2\u202fkm) away", "about 500m from the lake", "a 2km walk", "half a kilometre on",
                 "two kilometres of trees", "a mile from the gate"):
        assert prose_problems(text) == ["distance"], text
    assert prose_notes("A short hop away, 85% chance of rain") == ["85%"]
    # plain counts and unit-like words are allowed
    for text in ("Three museums and 2 movies nearby", "3 stops, all seated", "a calm stroll"):
        assert prose_problems(text) == [], text


def test_place_names_are_ignored_by_the_prose_rule() -> None:
    names = names_pattern(["Hotel 24 Hours", "Midnight Scoops", "The 10:30 Club", "Cafe 500m"])
    text = "Start at Hotel 24 Hours, then dessert at Midnight Scoops and a set at The 10:30 Club near Cafe 500m"
    assert set(prose_problems(text)) == {"number with unit", "clock time", "distance"}
    assert prose_problems(text, names) == []
    assert prose_problems("Hotel 24 Hours is 2 km away", names) == ["distance"]
    assert names_pattern([]) is None


async def test_an_underfilled_plan_needs_a_free_time_note(clean: Observation) -> None:
    obs = mutated(clean)
    plan = obs.result.plan  # type: ignore[union-attr]
    assert plan is not None
    obs.data["available_time"] = 600  # the same plan now covers well under 60% of the window
    obs.result.warnings = []  # type: ignore[union-attr]
    plan.summary_note = None
    assert "plan.uses_window" in failed(evaluate(obs))
    obs = mutated(obs)
    obs.result.warnings = ["This plan leaves about 5 h free after 3:30 PM — time to rest or wander nearby."]  # type: ignore[union-attr]
    assert "plan.uses_window" not in failed(evaluate(obs))
