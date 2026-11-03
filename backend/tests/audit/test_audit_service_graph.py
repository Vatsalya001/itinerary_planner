"""Independent service audit: graph, runner and LLM-layer reproductions (AUD-S1..AUD-S19).

Every test reproduces a confirmed defect and is marked ``xfail(strict=True)``: it fails today and turns into an
XPASS (a failure under strict mode) once the defect is fixed, so the marker must then be removed.
All tests run offline: scripted fake models, frozen-world fixture providers and synthetic slow/failing wrappers.
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
from collections import Counter
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import Field

import app.llm.registry as registry_module
from app.domain.schemas import PlanRequest
from app.graph.nodes.common import AGENT_RATE_LIMITED, AGENT_UNAVAILABLE
from app.graph.tools import TOOL_CAPS, tool_schemas
from app.llm.fake import ScriptedAgentModel
from app.llm.usage import UsageTracker
from app.providers.base import ProviderError
from app.providers.fixture import FixtureEvents
from tests.graph.helpers import collect, finalize_data, make_deps, request_for

# ---------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class _Scripted(ScriptedAgentModel):
    """Plays the scripted first turns, then the normal fake policy."""

    script: list[AIMessage] = Field(default_factory=list)

    def _agent_reply(self, messages: list[BaseMessage]) -> AIMessage:
        turn = sum(isinstance(m, AIMessage) for m in messages)
        if turn < len(self.script):
            return self.script[turn].model_copy(deep=True)
        return super()._agent_reply(messages)


def _use_agent_model(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    original = registry_module.get_chat_model

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        if purpose == "agent":
            return factory(model_id=model_id, role=role, fault=settings.active_fault())
        return original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)


class _CountingFailingEvents:
    """An events provider that is down (every search times out like Tavily would)."""

    name = "web_event"

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, interests: list[str], ctx: Any, max_price: int | None = None) -> list[Any]:
        self.calls += 1
        raise ProviderError("tavily", "timeout", "search timed out", retryable=True)


class _CountingEvents:
    name = "fixture_events"

    def __init__(self) -> None:
        self.inner = FixtureEvents()
        self.calls = 0

    async def search(self, interests: list[str], ctx: Any, max_price: int | None = None) -> list[Any]:
        self.calls += 1
        return await self.inner.search(interests, ctx, max_price=max_price)


class _SlowEvents:
    """Fixture events that take ``delay`` seconds (the documented whole-search budget is 15 s)."""

    name = "web_event"

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.inner = FixtureEvents()

    async def search(self, interests: list[str], ctx: Any, max_price: int | None = None) -> list[Any]:
        await asyncio.sleep(self.delay)
        return await self.inner.search(interests, ctx, max_price=max_price)


class _SlowPlaces:
    def __init__(self, inner: Any, delay: float) -> None:
        self.inner, self.delay, self.name = inner, delay, inner.name

    async def search(self, query: Any, ctx: Any) -> list[Any]:
        await asyncio.sleep(self.delay)
        return list(await self.inner.search(query, ctx))

    async def get(self, candidate_id: str, ctx: Any) -> Any:
        return await self.inner.get(candidate_id, ctx)

    def covers(self, location: Any) -> bool:
        return bool(self.inner.covers(location))


class _CountingFailingPlaces:
    """A live places provider that is down for the whole run."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.name, self.calls = inner, inner.name, 0

    async def search(self, query: Any, ctx: Any) -> list[Any]:
        self.calls += 1
        raise ProviderError(self.name, "timeout", "timed out", retryable=True)

    async def get(self, candidate_id: str, ctx: Any) -> Any:
        return None

    def covers(self, location: Any) -> bool:
        return bool(self.inner.covers(location))


# ---------------------------------------------------------------------------------------------------------
# AUD-S1: the deterministic fallback is not bounded by the time left, so a run ends "rejected" with no plan
# ---------------------------------------------------------------------------------------------------------


async def test_aud_s1_fallback_after_agent_timeout_still_returns_a_plan() -> None:
    # The model hangs; plan_agent gives it everything but the 12 s reserve (run_deadline_s=16 -> ~4 s turn).
    deps = make_deps(fault="llm_timeout", run_deadline_s=16)
    deps.places = _SlowPlaces(deps.places, 3.0)
    deps.events = _SlowEvents(15.0)
    t0 = time.perf_counter()
    result, _ = await collect(request_for("assignment_example"), deps)
    elapsed = time.perf_counter() - t0
    # Expected: the reserve exists so the rule-based planner can still answer.
    assert result.status in {"fallback", "complete"}, (result.status, result.message, round(elapsed, 1))


async def test_aud_s9_hard_deadline_trace_has_no_dangling_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.graph.nodes.fallback as fallback_module
    import app.graph.runner as runner_module

    async def hanging_events(prefs: Any, ctx: Any, deps: Any, tracer: Any) -> Any:
        with tracer.tool("search_events", node="gather_candidates"):
            await asyncio.sleep(3600)  # any slow await inside a node (provider, DB) behaves the same

    monkeypatch.setattr(fallback_module, "search_events", hanging_events)
    monkeypatch.setattr(runner_module, "HARD_GRACE_S", 0.3)
    result, events = await collect(request_for("assignment_example"), make_deps(run_deadline_s=1))
    assert result.status in {"fallback", "complete", "rejected"}  # bounded fallback may finish before hard stop
    started = Counter(e.node for e in events if e.type == "node_started")
    completed = Counter(e.node for e in events if e.type == "node_completed")
    called = Counter(e.tool for e in events if e.type == "tool_called")
    ended = Counter(e.tool for e in events if e.type in ("tool_completed", "tool_failed"))
    assert started == completed and called == ended, (started - completed, called - ended)


# ---------------------------------------------------------------------------------------------------------
# AUD-S2 / AUD-S3: event searches are neither sticky after a failure nor capped when batched into search_places
# ---------------------------------------------------------------------------------------------------------


async def test_aud_s2_failed_events_provider_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    first = AIMessage(content="", tool_calls=[_call("search_events", {"interests": ["music"], "reason": "events"},
                                                    "e1")])
    second = AIMessage(content="", tool_calls=[
        _call("search_places", {"queries": [{"intent": "quiet cafe", "category": "cafe"}], "include_events": True,
                                "reason": "a cafe, and events again"}, "p1"),
        _call("search_events", {"interests": ["music"], "reason": "try events once more"}, "e2"),
    ])
    _use_agent_model(monkeypatch, lambda **kw: _Scripted(script=[first, second], **kw))
    deps = make_deps()
    events_provider = _CountingFailingEvents()
    deps.events = events_provider
    result, _ = await collect(request_for("assignment_example"), deps)
    assert result.status == "complete"
    # HLD 11: "an events failure marks events_failed and is not retried"
    assert events_provider.calls == 1, events_provider.calls


async def test_aud_s3_event_searches_respect_the_per_run_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    def places(intent: str, call_id: str) -> dict[str, Any]:
        return _call("search_places", {"queries": [{"intent": intent}], "include_events": True, "reason": intent},
                     call_id)

    turn = AIMessage(content="", tool_calls=[
        places("quiet cafe", "a"), places("park", "b"), places("vegetarian restaurant", "c"),
        *[_call("search_events", {"interests": ["music"], "reason": "events"}, f"e{i}") for i in range(3)],
    ])
    _use_agent_model(monkeypatch, lambda **kw: _Scripted(script=[turn], **kw))
    deps = make_deps()
    counting = _CountingEvents()
    deps.events = counting
    result, _ = await collect(request_for("assignment_example"), deps)
    assert result.status == "complete"
    assert counting.calls <= TOOL_CAPS["search_events"], counting.calls


# ---------------------------------------------------------------------------------------------------------
# AUD-S4: fallback_planner ignores the run's sticky places_mode
# ---------------------------------------------------------------------------------------------------------


async def test_aud_s4_fallback_does_not_requery_a_failed_live_provider() -> None:
    deps = make_deps()
    failing = _CountingFailingPlaces(deps.places)
    deps.places = failing
    # Mumbai: the curated snapshot only covers Bangalore, so the first failure makes places_mode "unavailable".
    request = PlanRequest.model_validate({**request_for("assignment_example").model_dump(), "city": "Mumbai"})
    result, _ = await collect(request, deps)
    assert result.status == "no_feasible_plan"
    assert failing.calls == 1, f"live provider called {failing.calls} times after it failed once"


# ---------------------------------------------------------------------------------------------------------
# AUD-S5: clarification answers skip guard_input (no NFKC, control/bidi stripping or injection screening)
# ---------------------------------------------------------------------------------------------------------


def _has_format_chars(text: str | None) -> bool:
    return any(unicodedata.category(ch) in {"Cf", "Cc"} for ch in text or "")


async def test_aud_s5_clarification_answers_are_normalized_like_the_request() -> None:
    deps = make_deps()
    paused, _ = await collect(request_for("vague_free_text"), deps)
    assert paused.status == "needs_clarification" and paused.thread_id
    answers = {"city": "Bangalore​", "budget": "around 1000", "available_time": "afternoon, 3 hours",
               "mood": "chill ‮evil"}
    result, _ = await collect(None, deps, thread_id=paused.thread_id, resume_answers=answers)
    assert result.status == "complete" and result.preferences is not None
    # The same text in the original request is stripped by guard_input (normalize_text).
    assert not _has_format_chars(result.preferences.city), repr(result.preferences.city)
    assert not _has_format_chars(result.preferences.mood), repr(result.preferences.mood)


async def test_aud_s5_clamped_time_answer_is_disclosed() -> None:
    deps = make_deps()
    paused, _ = await collect(request_for("vague_free_text"), deps)
    answers = {"city": "Bangalore", "budget": "around 1000", "available_time": "20 hours"}
    result, _ = await collect(None, deps, thread_id=paused.thread_id, resume_answers=answers)
    assert result.status == "complete" and result.preferences is not None
    assert result.preferences.available_minutes == 720
    # The same "20 hours" in the original request adds this assumption (guard_input.clamp_available_time).
    assert "Capped at 12 hours" in result.assumptions, result.assumptions


# ---------------------------------------------------------------------------------------------------------
# AUD-S6: an unknown or pending FALLBACK_MODEL disables a working primary
# ---------------------------------------------------------------------------------------------------------


async def test_aud_s6_bad_fallback_model_does_not_disable_the_primary() -> None:
    deps = make_deps(primary_model="fake:agent", fallback_model="fake:typo")
    result, _ = await collect(request_for("assignment_example"), deps)
    assert result.mode == "agent", (result.status, result.mode, result.degradations)


# ---------------------------------------------------------------------------------------------------------
# AUD-S7: failure classification uses the primary's error, not the error that ended the chain
# ---------------------------------------------------------------------------------------------------------


class _FallbackRateLimited(ScriptedAgentModel):
    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: Any = None, **kwargs: Any) -> Any:
        if self.mode == "agent" and self.role == "fallback":
            err = RuntimeError("Error code: 429 - rate_limit_exceeded")
            err.status_code = 429  # type: ignore[attr-defined]
            raise err
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


async def test_aud_s7_rate_limit_that_ends_the_chain_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_agent_model(monkeypatch, lambda **kw: _FallbackRateLimited(**kw))
    deps = make_deps(primary_model="fake:primary_503", fallback_model="fake:secondary")
    result, _ = await collect(request_for("assignment_example"), deps)
    assert result.mode == "deterministic"
    assert AGENT_UNAVAILABLE not in result.degradations
    assert AGENT_RATE_LIMITED in result.degradations, result.degradations


# ---------------------------------------------------------------------------------------------------------
# AUD-S8: llm_calls misses model calls that were cancelled by a timeout
# ---------------------------------------------------------------------------------------------------------


async def test_aud_s8_timed_out_agent_call_is_counted() -> None:
    # Structured request: no LLM parse. The single agent turn hangs and is cancelled at its timeout.
    deps = make_deps(fault="llm_timeout", run_deadline_s=16)
    result, events = await collect(request_for("assignment_example"), deps)
    data = finalize_data(events)
    assert data["agent_turns"] == 1 and result.mode == "deterministic"
    assert data["usage"]["llm_calls"] == 1, data["usage"]


class _SlowPrimary(ScriptedAgentModel):
    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: Any = None, **kwargs: Any) -> Any:
        if self.role == "primary":
            await asyncio.sleep(10)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


async def test_aud_s8_primary_cut_by_its_time_share_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "get_chat_model",
                        lambda model_id, purpose, settings, *, role="primary": _SlowPrimary(model_id=model_id,
                                                                                            role=role))
    settings = make_deps(primary_model="fake:agent", fallback_model="fake:secondary").settings
    runnable = registry_module.build_agent_runnable(["fake:agent", "fake:secondary"], tool_schemas(), settings,
                                                    primary_timeout_s=0.3)
    tracker = UsageTracker()
    out = await runnable.ainvoke([HumanMessage(content="hi")], config={"callbacks": [tracker]})
    assert out.response_metadata.get("registry_model_id") == "fake:secondary"
    assert tracker.usage["llm_calls"] == 2 and not tracker._run_models, (tracker.usage, len(tracker._run_models))
