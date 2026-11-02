"""Case loading, merge semantics and per-case settings/deps for the eval harness."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from evals.cases import (
    HTTP_CASES,
    SUITES,
    CallLog,
    RecordingProvider,
    build_case_deps,
    case_settings,
    deadline_s,
    expand_placeholder,
    fixed_now,
    load_cases,
    model_kind,
    parse_case,
    request_data,
    select_cases,
)
from tests.evals.helpers import CASES, SPEC


def test_spec_cases_load_with_unique_ids_and_known_suites() -> None:
    cases = load_cases(SPEC)
    assert len({c.id for c in cases}) == len(cases) == len(SPEC["cases"])
    for c in cases:
        assert c.suites and set(c.suites) <= set(SUITES), c.id
        assert c.expected_statuses, c.id
        assert c.split in {"dev", "holdout"}, c.id


def test_merge_semantics_shallow_merge_arrays_replace_and_remove_fields() -> None:
    spec: dict[str, Any] = {"defaults": {"city": "Bangalore", "budget": 2000, "interests": ["food", "music"],
                                         "constraints": ["vegetarian"]}}
    case = parse_case({"id": "x", "suites": ["models"], "expected_statuses": ["complete"],
                       "input_overrides": {"interests": ["art"], "budget": 300}, "remove_fields": ["city"]})
    data = request_data(case, spec)
    assert data == {"budget": 300, "interests": ["art"], "constraints": ["vegetarian"]}
    assert spec["defaults"]["interests"] == ["food", "music"]  # defaults are never mutated


def test_use_defaults_false_sends_only_free_text() -> None:
    spec: dict[str, Any] = {"defaults": {"city": "Bangalore", "budget": 2000}}
    case = parse_case({"id": "x", "suites": ["models"], "expected_statuses": ["complete"], "use_defaults": False,
                       "free_text": "hello", "input_overrides": {"budget": 5}})
    assert request_data(case, spec) == {"free_text": "hello"}


def test_free_text_is_added_on_top_of_defaults() -> None:
    spec: dict[str, Any] = {"defaults": {"city": "Bangalore"}}
    case = parse_case({"id": "x", "suites": ["models"], "expected_statuses": ["complete"], "free_text": "hi"})
    assert request_data(case, spec) == {"city": "Bangalore", "free_text": "hi"}


def test_real_cases_merge_as_specified() -> None:
    assert request_data(CASES["missing_city_api"], SPEC).get("city") is None
    assert request_data(CASES["tight_budget_free_activities"], SPEC)["constraints"] == []
    vague = request_data(CASES["vague_free_text"], SPEC)
    assert vague == {"free_text": "plan something fun for saturday"}
    assert request_data(CASES["assignment_example"], SPEC) == SPEC["defaults"]


def test_oversize_placeholder_expands_to_20_kib() -> None:
    mood = request_data(CASES["oversize_body"], SPEC)["mood"]
    assert isinstance(mood, str) and len(mood) == 20 * 1024
    assert expand_placeholder("<2 KB string>") == "x" * 2048
    assert expand_placeholder("plain") == "plain"


def test_select_cases_by_suite_split_and_ids() -> None:
    cases = load_cases(SPEC)
    offline = select_cases(cases, "offline")
    assert len(offline) == sum(1 for c in SPEC["cases"] if "offline" in c["suites"])
    holdout = select_cases(cases, "models", "holdout")
    assert holdout and all(c.split == "holdout" for c in holdout)
    assert [c.id for c in select_cases(cases, "offline", ids=["vague_free_text"])] == ["vague_free_text"]
    assert select_cases(cases, "offline", ids=["holdout_vague_short"]) == []  # not in the offline suite
    with pytest.raises(ValueError, match="unknown case"):
        select_cases(cases, "offline", ids=["nope"])
    with pytest.raises(ValueError, match="unknown suite"):
        select_cases(cases, "nightly")


def test_case_flags() -> None:
    assert CASES["vague_free_text"].blocking and not CASES["vague_free_text"].feasible
    assert CASES["assignment_example"].feasible and not CASES["assignment_example"].blocking
    assert not CASES["wheelchair_access"].feasible  # no_feasible_plan is an acceptable outcome
    assert CASES["prompt_injection_in_mood"].feasible and not CASES["prompt_injection_in_mood"].blocking
    assert {c.id for c in CASES.values() if c.is_http} == set(HTTP_CASES)
    assert CASES["rate_limited"].app_fault is None  # simulated by the harness, never a settings.fault
    assert CASES["model_timeout"].app_fault == "llm_timeout"


def test_model_kind_and_deadlines() -> None:
    assert model_kind("baseline:deterministic") == "baseline"
    assert model_kind("ollama:granite4:tiny-h") == "local"
    assert model_kind("ollama:some-unlisted:model") == "local"
    assert model_kind("anthropic:claude-opus-5-5") == "api"
    assert model_kind("google_genai:anything") == "api"
    assert model_kind("fake:agent") == "fake"
    assert model_kind("nope:x") == "unknown"
    assert deadline_s("local", SPEC) == SPEC["deadlines_s"]["local"] == 240
    assert deadline_s("api", SPEC) == deadline_s("baseline", SPEC) == deadline_s("fake", SPEC) == 60


def test_case_settings_apply_fault_weather_config_clock_and_deadline() -> None:
    s = case_settings(CASES["provider_fallback_chain"], "fake:agent", SPEC)
    assert s.fault == "primary_provider_503" and s.fallback_model == "fake:secondary"
    assert s.primary_model == "fake:agent" and s.run_deadline_s == 60
    assert s.fixed_now == fixed_now(SPEC) == datetime.fromisoformat("2026-11-05T10:00:00+05:30")
    assert s.places_provider == "fixture" and s.events_provider == "fixture" and s.weather_provider == "fixture"
    rain = case_settings(CASES["rain_forecast"], "ollama:granite4:tiny-h", SPEC)
    assert rain.fixture_weather == "rain_85pct" and rain.run_deadline_s == 240 and rain.fault is None
    assert case_settings(CASES["rate_limited"], "fake:agent", SPEC).fault is None
    with pytest.raises(ValueError, match="not a setting"):
        case_settings(parse_case({"id": "x", "suites": ["models"], "expected_statuses": ["complete"],
                                  "config": {"NOT_A_SETTING": 1}}), "fake:agent", SPEC)


async def test_build_case_deps_installs_event_scenarios_and_recorders() -> None:
    case = CASES["prompt_injection_in_event_data"]
    deps, log = build_case_deps(case, case_settings(case, "fake:agent", SPEC))
    assert isinstance(deps.events, RecordingProvider) and isinstance(deps.places, RecordingProvider)
    assert deps.events._inner.scenarios == ["jsonld_name_with_injected_instructions"]
    assert deps.checkpointer is not None
    loc = (await deps.geocoder.resolve("Bangalore"))[0]
    assert log.calls["geocoder.resolve"] == 1 and loc.id not in log.ids  # locations are not candidates
    from app.domain.schemas import PlaceQuery

    ctx = _ctx(loc, deps)
    found = await deps.places.search(PlaceQuery(intent="cafe", category="cafe"), ctx)
    assert found and {c.id for c in found} <= log.ids and log.calls["places.search"] == 1


async def test_recording_provider_forwards_attributes() -> None:
    class Inner:
        name = "inner"

        def covers(self, location: Any) -> bool:
            return True

        async def get(self, cid: str, ctx: Any) -> None:
            return None

    log = CallLog()
    proxy = RecordingProvider(Inner(), "places", log)
    assert proxy.name == "inner" and proxy.covers(None) is True
    assert await proxy.get("x", None) is None
    assert log.total_calls == 1 and not log.ids


def _ctx(loc: Any, deps: Any) -> Any:
    from datetime import date

    from app.domain.pacing import profile_for
    from app.domain.schemas import Energy, Weather, WorldContext

    return WorldContext(location=loc, date=date(2026, 11, 7), start_min=900, end_min=1140,
                        weather=Weather(status="unavailable"), pacing=profile_for(Energy.LOW), now=deps.now())
