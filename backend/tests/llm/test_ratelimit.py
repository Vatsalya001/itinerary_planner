"""Client-side sliding-window rate limiting (app.llm.ratelimit) and its wiring through the registry."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from app.deps import fixture_settings
from app.graph.nodes.common import is_rate_limit_error
from app.graph.tools import tool_schemas
from app.llm import registry
from app.llm.fake import ScriptedAgentModel
from app.llm.ratelimit import RateLimitWaitError, SlidingWindowRateLimiter, call_budget, reset_limiters
from app.llm.registry import METADATA_KEY, build_agent_runnable, get_chat_model
from app.llm.usage import UsageTracker


@pytest.fixture(autouse=True)
def _fresh() -> None:
    reset_limiters()
    registry.clear_cache()


async def test_window_slides_without_bursts() -> None:
    limiter = SlidingWindowRateLimiter(2, 0.3)
    starts: list[float] = []

    async def call() -> None:
        await limiter.aacquire()
        starts.append(time.monotonic())

    await asyncio.gather(*(call() for _ in range(5)))
    starts.sort()
    for i in range(len(starts) - 2):  # any three consecutive starts span at least one window
        assert starts[i + 2] - starts[i] >= 0.29
    assert starts[-1] - starts[0] >= 0.59


async def test_waits_are_recorded_and_bookings_do_not_overlap() -> None:
    limiter = SlidingWindowRateLimiter(1, 0.2)
    waits: list[float] = []
    with call_budget(5, waits):
        await limiter.aacquire()
        await limiter.aacquire()
    assert len(waits) == 1 and 0.15 <= waits[0] <= 0.21


async def test_a_wait_longer_than_the_budget_fails_fast_as_a_rate_limit() -> None:
    limiter = SlidingWindowRateLimiter(1, 60)
    await limiter.aacquire()
    t0 = time.monotonic()
    with call_budget(5), pytest.raises(RateLimitWaitError) as err:
        await limiter.aacquire()
    assert time.monotonic() - t0 < 0.1
    assert is_rate_limit_error(err.value) and err.value.status_code == 429
    assert len(limiter._slots) == 1  # nothing was booked for the refused call


async def test_nested_budget_only_shortens() -> None:
    limiter = SlidingWindowRateLimiter(1, 3)
    await limiter.aacquire()
    with call_budget(60), call_budget(1), pytest.raises(RateLimitWaitError):
        await limiter.aacquire()  # the inner (primary-share) budget applies


def test_production_models_get_shared_limiters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-not-a-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-not-a-key")
    settings = fixture_settings()
    agent = get_chat_model("cerebras:gpt-oss-120b", "agent", settings)
    parse = get_chat_model("cerebras:gpt-oss-120b", "parse", settings)
    judge = get_chat_model("cerebras:gpt-oss-120b", "judge", settings)
    assert isinstance(agent.rate_limiter, SlidingWindowRateLimiter)
    assert agent.rate_limiter is parse.rate_limiter is judge.rate_limiter
    assert (agent.rate_limiter.requests, agent.rate_limiter.window_s) == (5, 60.0)
    groq = get_chat_model("groq:openai/gpt-oss-120b", "agent", settings)
    assert isinstance(groq.rate_limiter, SlidingWindowRateLimiter) and groq.rate_limiter.requests == 30
    assert get_chat_model("fake:agent", "agent", settings).rate_limiter is None


async def test_a_full_primary_window_moves_the_call_to_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    full = SlidingWindowRateLimiter(1, 60, name="fake:agent")
    await full.aacquire()

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        model = ScriptedAgentModel(model_id=model_id, role=role)
        return model.model_copy(update={"rate_limiter": full if model_id == "fake:agent" else None})

    monkeypatch.setattr(registry, "get_chat_model", fake_get)
    settings = fixture_settings(primary_model="fake:agent", fallback_model="fake:secondary")
    runnable = build_agent_runnable(["fake:agent", "fake:secondary"], tool_schemas(), settings, primary_timeout_s=10)
    tracker = UsageTracker()
    t0 = time.monotonic()
    with call_budget(30):
        message = await runnable.ainvoke([HumanMessage(content="{}")], config={"callbacks": [tracker]})
    assert time.monotonic() - t0 < 1  # no 60 s wait: the primary's share can't cover it
    assert message.response_metadata[METADATA_KEY] == "fake:secondary"
    assert tracker.usage["llm_calls"] == 1  # the refused primary call never reached a provider
