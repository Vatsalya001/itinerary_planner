"""Helpers for the offline graph tests: eval-case requests, frozen-world deps, trace and plan assertions.

Plan assertions here recompute from the fixture records and the plan's own legs (not from the planner's
check flags) where that is cheap: worst-case cost, elapsed time, diet/wheelchair evidence, crowds, pacing.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.deps import Deps, build_deps, fixture_settings
from app.domain.evidence import is_food_stop, is_outdoor, is_walking_stop, likely_busy, opening_status
from app.domain.schemas import Evidence, PlanRequest, PlanResult, TraceEvent
from app.domain.validator import numeric_claims
from app.graph.runner import run_plan, stream_plan
from app.providers.fixture import FixtureEvents

EVAL_CASES_PATH = Path(__file__).resolve().parents[3] / "eval_cases.json"


def load_cases() -> dict[str, Any]:
    return json.loads(EVAL_CASES_PATH.read_text(encoding="utf-8"))


CASES = load_cases()
CASE_BY_ID = {c["id"]: c for c in CASES["cases"]}


def offline_case_ids() -> list[str]:
    return [c["id"] for c in CASES["cases"] if "offline" in c["suites"]]


def request_for(case_id: str) -> PlanRequest:
    """PlanRequest per eval_cases.json merge semantics (defaults + overrides, remove_fields, use_defaults)."""
    case = CASE_BY_ID[case_id]
    if case.get("use_defaults", True):
        data: dict[str, Any] = dict(CASES["defaults"])
        data.update(case.get("input_overrides") or {})
        for key in case.get("remove_fields") or []:
            data.pop(key, None)
    else:
        data = {}
    if case.get("free_text"):
        data["free_text"] = case["free_text"]
    return PlanRequest.model_validate(data)


def make_deps(**overrides: Any) -> Deps:
    """Frozen-world deps with the scripted fake model and a fresh in-memory checkpointer."""
    events = overrides.pop("events", None)
    overrides.setdefault("primary_model", "fake:agent")
    deps = build_deps(fixture_settings(**overrides))
    if events is not None:
        deps.events = FixtureEvents(events)
    deps.checkpointer = InMemorySaver()
    return deps


def deps_for_case(case_id: str, **overrides: Any) -> Deps:
    case = CASE_BY_ID[case_id]
    fixtures = case.get("fixtures") or {}
    if "weather" in fixtures:
        overrides.setdefault("fixture_weather", fixtures["weather"])
    if "events" in fixtures:
        overrides.setdefault("events", fixtures["events"])
    if case.get("fault"):
        overrides.setdefault("fault", case["fault"])
    config = case.get("config") or {}
    if "FALLBACK_MODEL" in config:
        overrides.setdefault("fallback_model", config["FALLBACK_MODEL"])
    return make_deps(**overrides)


async def run_case(case_id: str, **overrides: Any) -> tuple[PlanResult, list[TraceEvent], Deps]:
    deps = deps_for_case(case_id, **overrides)
    result, events = await run_plan(request_for(case_id), deps)
    return result, events, deps


async def collect(request: PlanRequest | None, deps: Deps, **kwargs: Any) -> tuple[PlanResult, list[TraceEvent]]:
    events = [e async for e in stream_plan(request, deps, **kwargs)]
    results = [e for e in events if e.type == "result"]
    assert len(results) == 1
    return PlanResult.model_validate(results[0].data), events


# ---------------------------------------------------------------------------------------------------
# Trace assertions
# ---------------------------------------------------------------------------------------------------


def finalize_data(events: list[TraceEvent]) -> dict[str, Any]:
    fin = [e for e in events if e.type == "node_completed" and e.node == "finalize"]
    assert len(fin) == 1
    return fin[0].data or {}


def agent_tools(events: list[TraceEvent]) -> Counter[str]:
    return Counter(e.tool for e in events if e.type == "tool_called" and e.node == "tools" and e.tool)


def nodes_run(events: list[TraceEvent]) -> list[str]:
    return [e.node for e in events if e.type == "node_started" and e.node]


def assert_trace_integrity(events: list[TraceEvent]) -> None:
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be strictly increasing"
    assert len({e.run_id for e in events}) == 1
    assert events[0].type == "run_started"
    results = [i for i, e in enumerate(events) if e.type == "result"]
    assert results == [len(events) - 1], "exactly one result, and it is last"
    before = events[-2]
    assert before.type == "node_completed" and before.node == "finalize"
    data = before.data or {}
    for key in ("usage", "submit_attempts", "tool_calls", "model", "protocol_errors", "repairs",
                "first_submit_valid", "nudges", "agent_turns"):
        assert key in data, key
    assert set(data["usage"]) >= {"input_tokens", "output_tokens", "cache_read_tokens", "cost_usd", "llm_calls"}
    called = Counter(e.tool for e in events if e.type == "tool_called")
    ended = Counter(e.tool for e in events if e.type in ("tool_completed", "tool_failed"))
    assert called == ended, f"unmatched tool events: called={called} ended={ended}"
    started = Counter(e.node for e in events if e.type == "node_started")
    completed = Counter(e.node for e in events if e.type == "node_completed")
    assert started == completed


def events_json(events: list[TraceEvent]) -> str:
    return "\n".join(e.model_dump_json() for e in events)


# ---------------------------------------------------------------------------------------------------
# Plan assertions (recomputed from records)
# ---------------------------------------------------------------------------------------------------


def assert_plan_sound(result: PlanResult) -> None:
    plan = result.plan
    prefs = result.preferences
    assert plan is not None and prefs is not None
    ids = [s.candidate.id for s in plan.stops]
    assert len(ids) == len(set(ids)), "duplicate stops"
    stops_max = sum(s.candidate.cost.max for s in plan.stops)
    legs_max = sum(leg.fare.max for leg in plan.legs)
    worst = math.ceil((stops_max + legs_max) * 1.10)
    if prefs.budget is not None:
        assert worst <= prefs.budget, f"worst case {worst} above budget {prefs.budget}"
    elapsed = plan.stops[-1].end_min - plan.start_min
    assert elapsed <= prefs.available_minutes
    keys = {c.key for c in prefs.hard_constraints}
    for s in plan.stops:
        c = s.candidate
        assert s.why.strip()
        assert not numeric_claims(s.why) and not numeric_claims(s.tradeoff)
        assert c.source.provider
        assert opening_status(c, s.start_min, s.end_min) != "closed"
        if "diet:vegetarian" in keys and is_food_stop(c):
            assert c.diet.get("vegetarian") == Evidence.CONFIRMED
        if "diet:vegan" in keys and is_food_stop(c):
            assert c.diet.get("vegan") == Evidence.CONFIRMED
        if "access:wheelchair" in keys:
            assert c.wheelchair == Evidence.CONFIRMED
        if "crowd:avoid" in keys:
            assert not likely_busy(c, s.start_min, s.end_min)
        sunset = plan.weather.sunset_min
        if sunset is not None and is_outdoor(c) and is_walking_stop(c):
            assert s.end_min <= sunset
    assert not numeric_claims(plan.summary_note)
    pacing_max = {"low": 3, "medium": 4, "high": 6}[prefs.energy.value]
    assert len(plan.stops) <= pacing_max
    if prefs.energy.value == "low":
        assert any(s.candidate.seated for s in plan.stops)
    assert not any(check.status == "fail" for check in result.checks)
