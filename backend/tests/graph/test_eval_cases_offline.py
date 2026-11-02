"""One test per eval_cases.json "offline" case the graph is responsible for (scripted fake model, frozen world).

Cases owned elsewhere: negative_budget, oversize_body, rate_limited, share_link_roundtrip and xss_in_input are
API/UI behaviours (HTTP 400/413/429, share links, Playwright rendering) and are tested by the API and UI suites.
"""

from __future__ import annotations

import time

import pytest

from app.domain.evidence import is_food_stop, is_outdoor
from app.domain.schemas import Evidence
from app.graph.nodes.common import (
    AGENT_NO_SUBMIT,
    AGENT_REFUSED,
    AGENT_TIMEOUT,
    OUT_OF_SCOPE_ASSUMPTION,
    WEATHER_ASSUMPTION,
)
from app.graph.prompts import AGENT_SYSTEM_PROMPT
from app.pipeline import EVENTS_UNAVAILABLE, SNAPSHOT_USED
from tests.graph.helpers import (
    agent_tools,
    assert_plan_sound,
    assert_trace_integrity,
    collect,
    deps_for_case,
    events_json,
    finalize_data,
    nodes_run,
    offline_case_ids,
    request_for,
    run_case,
)

GRAPH_OWNED = {
    "assignment_example", "city_alias_bengaluru", "free_text_complete", "benign_ignore_phrase", "wheelchair_access",
    "vague_free_text", "clarification_answered", "missing_city_api", "currency_ambiguity_abroad", "unknown_city",
    "contradictory_constraints", "impossible_budget", "late_night_start", "rain_forecast", "weather_unavailable",
    "event_grounding", "model_timeout", "provider_fallback_chain", "model_refusal",
    "invalid_submission_then_repair_exhausted", "agent_ends_without_submit", "places_provider_outage",
    "places_outage_no_snapshot_city", "event_search_failure", "diet_dropped_by_parser", "prompt_injection_in_mood",
    "prompt_injection_in_event_data",
}
NOT_GRAPH = {"negative_budget", "oversize_body", "rate_limited", "share_link_roundtrip", "xss_in_input"}


def test_every_offline_case_is_accounted_for() -> None:
    assert set(offline_case_ids()) == GRAPH_OWNED | NOT_GRAPH


async def test_assignment_example() -> None:
    result, events, _ = await run_case("assignment_example")
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "agent" and result.model == "fake:agent"
    assert len(set(agent_tools(events))) >= 3
    validations = [e for e in events if e.type == "validation"]
    assert validations and validations[-1].data and validations[-1].data["ok"] is True
    assert_plan_sound(result)
    assert result.plan is not None and len(result.plan.stops) <= 3
    assert all(c.status in {"pass", "warn", "unverified"} for c in result.checks)
    data = finalize_data(events)
    assert data["first_submit_valid"] is True and data["repairs"] == 0 and data["protocol_errors"] == 0
    # structured request: no parse call; one batched search turn and one submit turn
    assert data["usage"]["llm_calls"] == 2 and data["agent_turns"] == 2
    assert agent_tools(events)["search_places"] == 1 and "check_plan" not in agent_tools(events)


async def test_city_alias_bengaluru() -> None:
    alias, events, _ = await run_case("city_alias_bengaluru")
    base, _, _ = await run_case("assignment_example")
    assert_trace_integrity(events)
    assert alias.status == "complete"
    assert alias.plan is not None and base.plan is not None
    assert alias.plan.timezone == base.plan.timezone == "Asia/Kolkata"
    assert alias.plan.totals.currency == base.plan.totals.currency == "INR"
    assert {s.candidate.city_id for s in alias.plan.stops} == {s.candidate.city_id for s in base.plan.stops}


async def test_free_text_complete() -> None:
    result, events, _ = await run_case("free_text_complete")
    assert_trace_integrity(events)
    assert result.status == "complete"
    assert not [e for e in events if e.type == "clarification"]
    prefs = result.preferences
    assert prefs is not None
    assert prefs.budget == 1500 and prefs.currency == "INR"
    assert prefs.start_min == 16 * 60 and prefs.available_minutes == 240
    assert prefs.neighbourhood == "Indiranagar" and prefs.energy.value == "low"
    assert "diet:vegan" in {c.key for c in prefs.hard_constraints}
    assert {"coffee", "books"} <= set(prefs.interests)
    assert_plan_sound(result)
    assert result.plan is not None
    for stop in result.plan.stops:
        if is_food_stop(stop.candidate):
            assert stop.candidate.diet.get("vegan") == Evidence.CONFIRMED


async def test_benign_ignore_phrase() -> None:
    result, events, _ = await run_case("benign_ignore_phrase")
    assert_trace_integrity(events)
    assert result.status == "complete"
    guard = next(e for e in events if e.type == "node_completed" and e.node == "guard_input")
    assert guard.data and guard.data["injection_flagged"] is False
    assert_plan_sound(result)


async def test_wheelchair_access() -> None:
    result, events, _ = await run_case("wheelchair_access")
    assert_trace_integrity(events)
    assert result.status in {"complete", "no_feasible_plan"}
    if result.status == "complete":
        assert_plan_sound(result)
        assert result.plan is not None
        assert all(s.candidate.wheelchair == Evidence.CONFIRMED for s in result.plan.stops)


async def test_vague_free_text() -> None:
    result, events, _ = await run_case("vague_free_text")
    assert_trace_integrity(events)
    assert result.status == "needs_clarification"
    assert result.thread_id and result.plan is None
    assert 1 <= len(result.questions) <= 2
    assert any(q.id == "city" and q.options for q in result.questions)
    assert "plan_agent" not in nodes_run(events)
    assert [e for e in events if e.type == "clarification"]


async def test_clarification_answered() -> None:
    deps = deps_for_case("clarification_answered")
    first, _ = await collect(request_for("clarification_answered"), deps)
    assert first.status == "needs_clarification" and first.thread_id
    answers = {"city": "Bangalore", "budget": "around 1000", "available_time": "afternoon, 3 hours"}
    result, events = await collect(None, deps, thread_id=first.thread_id, resume_answers=answers)
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "agent"
    assert not [e for e in events if e.type == "clarification"]
    assert events[0].data and events[0].data["resumed"] is True
    assert result.preferences is not None and result.preferences.budget == 1000
    assert result.preferences.available_minutes == 180
    assert result.assumptions  # defaults for soft preferences are listed
    assert_plan_sound(result)
    # the thread is gone after the terminal result: resuming again is rejected, not re-planned
    again, _ = await collect(None, deps, thread_id=first.thread_id, resume_answers=answers)
    assert again.status == "rejected"


async def test_missing_city_api() -> None:
    result, events, _ = await run_case("missing_city_api")
    assert_trace_integrity(events)
    assert result.status == "needs_clarification"
    assert any(q.id == "city" for q in result.questions)
    assert "plan_agent" not in nodes_run(events)


async def test_currency_ambiguity_abroad() -> None:
    result, events, _ = await run_case("currency_ambiguity_abroad")
    assert_trace_integrity(events)
    assert result.status == "needs_clarification" and result.plan is None
    question = next(q for q in result.questions if q.id == "currency")
    assert set(question.options) >= {"GBP", "INR"}


async def test_unknown_city() -> None:
    result, events, _ = await run_case("unknown_city")
    assert_trace_integrity(events)
    assert result.status == "needs_clarification" and result.plan is None
    question = next(q for q in result.questions if q.id == "city_not_found")
    assert "Atlantisville" in question.question
    assert not [e for e in events if e.tool == "search_places"]


async def test_contradictory_constraints() -> None:
    result, events, _ = await run_case("contradictory_constraints")
    assert_trace_integrity(events)
    assert result.status == "needs_clarification" and result.plan is None
    assert result.preferences is not None
    keys = {c.key for c in result.preferences.hard_constraints}
    assert {"setting:indoors_only", "must_include:outdoor_walk"} <= keys
    question = next(q for q in result.questions if q.id == "conflict")
    assert len(question.options) == 2


async def test_impossible_budget() -> None:
    result, events, _ = await run_case("impossible_budget")
    assert_trace_integrity(events)
    assert result.status == "no_feasible_plan" and result.plan is None
    assert not [e for e in events if e.type == "clarification"]
    assert result.preferences is not None
    keys = {c.key for c in result.preferences.hard_constraints}
    assert {"diet:vegetarian", "access:wheelchair", "must_include:sit_down_meal"} <= keys
    assert result.infeasible is not None and result.infeasible.cheapest_budget is not None
    assert any("budget" in r.patch for r in result.infeasible.relaxations)
    # the model's "nothing fits" was confirmed by the deterministic search, so it is not a degradation
    assert AGENT_NO_SUBMIT not in result.degradations
    assert not [e for e in events if e.type == "degraded"]


async def test_late_night_start() -> None:
    result, events, _ = await run_case("late_night_start")
    assert_trace_integrity(events)
    assert result.status in {"complete", "no_feasible_plan"}
    if result.plan is not None:
        assert_plan_sound(result)


async def test_rain_forecast() -> None:
    result, events, _ = await run_case("rain_forecast")
    assert_trace_integrity(events)
    assert result.status == "complete" and result.plan is not None
    assert result.plan.weather.is_rainy
    for stop in result.plan.stops:
        if is_outdoor(stop.candidate):
            assert stop.plan_b is not None and stop.plan_b.indoor is True
    assert any("rain" in s.why.lower() or "indoor" in s.why.lower() for s in result.plan.stops)
    assert_plan_sound(result)


async def test_weather_unavailable() -> None:
    result, events, _ = await run_case("weather_unavailable")
    assert_trace_integrity(events)
    assert result.status == "complete"
    assert WEATHER_ASSUMPTION in result.assumptions
    assert any(e.type == "degraded" and e.node == "get_weather" and (e.data or {}).get("recovered") for e in events)
    assert_plan_sound(result)


async def test_event_grounding() -> None:
    result, events, _ = await run_case("event_grounding")
    assert_trace_integrity(events)
    assert result.status == "complete" and result.plan is not None
    # of the three fixture events only the same-day one with an https source survives the provider rules
    searched = [e for e in events if e.type == "tool_completed" and e.tool == "search_events"]
    assert searched and all((e.data or {}).get("count") == 1 for e in searched)
    for stop in result.plan.stops:
        c = stop.candidate
        if c.kind == "event":
            assert c.event_date == result.plan.date
            assert c.source.url and c.source.url.startswith("https://events.example.com/")
            assert c.name == "Indie Acoustic Evening"
    assert_plan_sound(result)


async def test_model_timeout() -> None:
    deadline_s = 16
    t0 = time.perf_counter()
    result, events, _ = await run_case("model_timeout", run_deadline_s=deadline_s)
    wall = time.perf_counter() - t0
    assert_trace_integrity(events)
    assert result.status == "fallback" and result.mode == "deterministic"
    assert AGENT_TIMEOUT in result.degradations
    assert wall < deadline_s and (result.latency_ms or 0) < deadline_s * 1000
    assert_plan_sound(result)


async def test_provider_fallback_chain() -> None:
    result, events, _ = await run_case("provider_fallback_chain")
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "agent"
    assert result.model == "fake:secondary"
    assert finalize_data(events)["model"] == "fake:secondary"
    switch = [e for e in events if e.type == "degraded" and "switched to fake:secondary" in (e.summary or "")]
    assert switch
    assert_plan_sound(result)


async def test_provider_fallback_chain_with_failing_model_id() -> None:
    result, events, _ = await run_case("provider_fallback_chain", primary_model="fake:primary_503", fault=None)
    assert result.status == "complete" and result.model == "fake:secondary"
    assert_trace_integrity(events)


async def test_model_refusal() -> None:
    result, events, _ = await run_case("model_refusal")
    assert_trace_integrity(events)
    assert result.status == "fallback" and result.mode == "deterministic"
    assert AGENT_REFUSED in result.degradations
    assert not [e for e in events if e.tool == "submit_plan"]
    assert_plan_sound(result)


async def test_invalid_submission_then_repair_exhausted() -> None:
    result, events, _ = await run_case("invalid_submission_then_repair_exhausted")
    assert_trace_integrity(events)
    assert result.status == "fallback" and result.mode == "deterministic"
    data = finalize_data(events)
    assert data["repairs"] <= 2 and data["submit_attempts"] == 3 and data["first_submit_valid"] is False
    assert data["protocol_error_kinds"] == {"unknown_id": 3}  # one per submission that cited unknown IDs
    rejected = [e for e in events if e.type == "validation" and e.node == "validate_plan"]
    assert len(rejected) == 3
    assert all(e.data and not e.data["ok"] and e.data["violations"][0]["code"] == "UNKNOWN_ID" for e in rejected)
    failures = [e for e in events if e.type == "tool_failed" and e.tool == "submit_plan"]
    assert len(failures) == 3
    assert "invented-place" not in events_json(events)  # unknown IDs never displayed
    assert_plan_sound(result)


async def test_agent_ends_without_submit() -> None:
    from app.graph.nodes.common import AGENT_CHECKED_NOT_SUBMITTED

    result, events, _ = await run_case("agent_ends_without_submit")
    assert_trace_integrity(events)
    assert result.status in {"complete", "fallback"}
    # one nudge, then the draft that already passed check_plan is re-validated and used (never the free text)
    assert result.status == "complete" and result.mode == "agent"
    assert finalize_data(events)["nudges"] == 1 and finalize_data(events)["submit_attempts"] == 0
    assert result.degradations == [AGENT_CHECKED_NOT_SUBMITTED]
    assert "Here is my plan" not in events_json(events)
    assert_plan_sound(result)


async def test_places_provider_outage() -> None:
    result, events, _ = await run_case("places_provider_outage")
    assert_trace_integrity(events)
    assert result.status in {"complete", "fallback"} and result.plan is not None
    assert SNAPSHOT_USED in result.degradations
    for stop in result.plan.stops:
        assert stop.candidate.source.provider == "curated_snapshot"
        assert stop.candidate.source.last_checked is not None
    assert_plan_sound(result)


async def test_places_outage_no_snapshot_city() -> None:
    result, events, _ = await run_case("places_outage_no_snapshot_city")
    assert_trace_integrity(events)
    assert result.status == "no_feasible_plan" and result.plan is None
    assert result.message and "temporarily unavailable" in result.message
    assert finalize_data(events)["agent_turns"] == 1  # no pointless turns once places are known to be down


async def test_event_search_failure() -> None:
    result, events, _ = await run_case("event_search_failure")
    assert_trace_integrity(events)
    assert result.status == "complete" and result.plan is not None
    assert all(s.candidate.kind != "event" for s in result.plan.stops)
    assert EVENTS_UNAVAILABLE in result.degradations
    assert any(e.type == "tool_failed" and e.tool == "search_events" for e in events)


async def test_diet_dropped_by_parser() -> None:
    result, events, _ = await run_case("diet_dropped_by_parser")
    assert_trace_integrity(events)
    assert result.status in {"complete", "fallback", "no_feasible_plan"}
    codes = {v["code"] for e in events if e.type == "validation" for v in (e.data or {}).get("violations", [])}
    assert codes & {"CONSTRAINT_DROPPED", "DIET_CONFLICT"}
    assert result.preferences is not None
    assert "diet:vegetarian" in {c.key for c in result.preferences.hard_constraints}
    if result.plan is not None:
        for stop in result.plan.stops:
            if is_food_stop(stop.candidate):
                assert stop.candidate.diet.get("vegetarian") == Evidence.CONFIRMED
        assert_plan_sound(result)


async def test_prompt_injection_in_mood(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-SECRET-SENTINEL")
    result, events, _ = await run_case("prompt_injection_in_mood")
    assert_trace_integrity(events)
    assert result.status in {"complete", "needs_clarification"}
    dumped = events_json(events)
    assert "sk-SECRET-SENTINEL" not in dumped
    assert AGENT_SYSTEM_PROMPT[:80] not in dumped
    assert "planning agent of the Perfect Saturday Planner" not in dumped
    guard = next(e for e in events if e.type == "node_completed" and e.node == "guard_input")
    assert guard.data and guard.data["injection_flagged"] is True
    if result.status == "complete":
        assert_plan_sound(result)
        assert result.plan is not None and result.preferences is not None
        assert result.plan.totals.worst_case <= (result.preferences.budget or 0)


async def test_prompt_injection_in_event_data() -> None:
    result, events, _ = await run_case("prompt_injection_in_event_data")
    assert_trace_integrity(events)
    assert result.status == "complete" and result.plan is not None
    assert_plan_sound(result)
    assert not any(c.status == "fail" for c in result.checks)
    assert "reveal the system prompt" not in (result.plan.summary_note or "")
    assert result.preferences is not None and result.plan.totals.worst_case <= (result.preferences.budget or 0)
    # the injected event reached the agent only as clipped data (see test_tools for the 120-character cap)
    assert any(e.type == "tool_completed" and e.tool == "search_events" for e in events)


async def test_out_of_scope_request_gets_assumption_and_plan() -> None:
    from app.domain.schemas import PlanRequest
    from tests.graph.helpers import make_deps

    request = request_for("assignment_example").model_copy(
        update={"free_text": "Can you book a table for two and pay for the tickets? I love bookstores."})
    result, events = await collect(PlanRequest.model_validate(request.model_dump()), make_deps())
    assert result.status == "complete"
    assert OUT_OF_SCOPE_ASSUMPTION in result.assumptions
    assert_trace_integrity(events)
