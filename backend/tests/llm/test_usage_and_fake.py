"""Usage accounting, pricing and the scripted fake model."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.deps import fixture_settings
from app.domain.schemas import PlanRequest
from app.graph.prompts import LLMParse, agent_payload, agent_payload_message, parse_messages
from app.graph.tools import tool_schemas
from app.llm import registry
from app.llm.fake import FakeProviderHTTPError, ScriptedAgentModel
from app.llm.pricing import Pricing, pricing_for
from app.llm.usage import UsageTracker, add_usage, empty_usage, message_usage
from tests.factories import NOW, make_ctx, make_prefs


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    registry.clear_cache()


def test_pricing_cost_math() -> None:
    p = Pricing(input=4.0, output=20.0, cache_read=0.2, cache_write=5.0)
    cost = p.cost_usd(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=400_000,
                      cache_write_tokens=100_000)
    assert cost == pytest.approx((500_000 * 4 + 400_000 * 0.2 + 100_000 * 5 + 100_000 * 20) / 1e6)
    assert Pricing().cost_usd(10**6, 10**6) == 0
    assert pricing_for("anthropic:claude-opus-5-5").output == 20.0
    assert pricing_for("ollama:qwen3.5:9b").input == 0 and pricing_for("nope:x").input == 0


def test_message_usage_and_reducer() -> None:
    msg = AIMessage(content="x", usage_metadata={
        "input_tokens": 10_000, "output_tokens": 2_000, "total_tokens": 12_000,
        "input_token_details": {"cache_read": 8_000, "cache_creation": 0}})
    usage = message_usage(msg, "anthropic:claude-opus-5-5")
    assert usage["cache_read_tokens"] == 8_000
    assert usage["cost_usd"] == pytest.approx((2_000 * 4 + 8_000 * 0.2 + 2_000 * 20) / 1e6)
    total = add_usage(add_usage(empty_usage(), usage), {"llm_calls": 2})
    assert total["input_tokens"] == 10_000 and total["llm_calls"] == 2


async def test_usage_tracker_counts_calls_and_tokens() -> None:
    settings = fixture_settings(primary_model="fake:agent")
    runnable = registry.build_agent_runnable(["fake:agent"], tool_schemas(), settings)
    payload = agent_payload(make_prefs(), make_ctx(), [], {"search_places": 6}, True)
    tracker = UsageTracker()
    await runnable.ainvoke([agent_payload_message(payload)], config={"callbacks": [tracker]})
    assert tracker.usage["llm_calls"] == 1 and tracker.usage["input_tokens"] > 0
    assert tracker.models == ["fake:agent"]
    failing = registry.build_agent_runnable(["fake:primary_503"], tool_schemas(), settings)
    with pytest.raises(FakeProviderHTTPError):
        await failing.ainvoke([HumanMessage(content="{}")], config={"callbacks": [tracker]})
    assert tracker.usage["llm_calls"] == 2


def test_bind_tools_returns_a_bound_copy() -> None:
    base = ScriptedAgentModel()
    bound = base.bind_tools(tool_schemas())
    assert bound is not base and isinstance(bound, ScriptedAgentModel)
    assert bound.mode == "agent" and bound.tool_names[0] == "search_places" and base.mode == "chat"


async def test_scripted_first_turn_searches_with_reasons() -> None:
    model = ScriptedAgentModel().bind_tools(tool_schemas())
    payload = agent_payload(make_prefs(constraints=["vegetarian"]), make_ctx(), [], {"search_places": 6}, True)
    reply = await model.ainvoke([agent_payload_message(payload)])
    names = [tc["name"] for tc in reply.tool_calls]
    assert names.count("search_places") >= 3 and names[-1] == "search_events"
    assert all(tc["args"]["reason"] for tc in reply.tool_calls)


async def test_structured_parse_mirrors_rules_and_diet_fault() -> None:
    request = PlanRequest(city="Bangalore", budget=2000, mood="tired", constraints=["vegetarian"])
    normal = ScriptedAgentModel().with_structured_output(LLMParse, include_raw=True)
    out = await normal.ainvoke(parse_messages(request, NOW))
    assert out["parsing_error"] is None and out["parsed"].city == "Bangalore"
    assert [c.kind for c in out["parsed"].hard_constraints] == ["diet"]
    faulty = ScriptedAgentModel(fault="parser_omits_diet_and_agent_selects_nonveg").with_structured_output(LLMParse)
    parsed = await faulty.ainvoke(parse_messages(request, NOW))
    assert parsed.hard_constraints == []


async def test_faults() -> None:
    hanging = ScriptedAgentModel(fault="llm_timeout").bind_tools(tool_schemas())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(hanging.ainvoke([HumanMessage(content="{}")]), 0.05)
    refusing = ScriptedAgentModel(fault="llm_refusal").bind_tools(tool_schemas())
    reply = await refusing.ainvoke([HumanMessage(content="{}")])
    assert reply.response_metadata["stop_reason"] == "refusal" and not reply.tool_calls
    primary = ScriptedAgentModel(fault="primary_provider_503", role="primary").bind_tools(tool_schemas())
    with pytest.raises(FakeProviderHTTPError) as err:
        await primary.ainvoke([HumanMessage(content="{}")])
    assert err.value.status_code == 503
    secondary = ScriptedAgentModel(model_id="fake:secondary", fault="primary_provider_503", role="fallback")
    reply = await secondary.bind_tools(tool_schemas()).ainvoke([HumanMessage(content="{}")])
    assert isinstance(reply, AIMessage)
    assert json.loads(json.dumps(reply.response_metadata))["model_name"] == "fake:secondary"
