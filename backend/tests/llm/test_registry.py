"""Model registry: provider parameter rules, pending/unknown IDs, chains with fallbacks, refusal handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama

from app.deps import fixture_settings
from app.graph.prompts import AGENT_SYSTEM_PROMPT, LLMParse
from app.graph.tools import tool_schemas
from app.llm import registry
from app.llm.fake import ScriptedAgentModel
from app.llm.registry import (
    METADATA_KEY,
    ModelRefusalError,
    PendingModelError,
    RegistryError,
    UnknownModelError,
    build_agent_runnable,
    build_structured_runnable,
    get_chat_model,
    get_spec,
    is_refusal,
    load_registry,
    model_chain,
    provider_kwargs,
)

OPUS = "anthropic:claude-opus-5-5"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    registry.clear_cache()


def test_registry_lists_the_model_matrix() -> None:
    reg = load_registry()
    cases = json.loads((Path(__file__).resolve().parents[3] / "eval_cases.json").read_text())
    for row in cases["model_matrix"]:
        if "<" in row["id"]:
            continue
        assert row["id"] in reg.specs, row["id"]
    assert reg.production_primary == "groq:openai/gpt-oss-120b"
    assert reg.production_fallback is None
    groq = reg.specs["groq:openai/gpt-oss-120b"]
    assert groq.kind == "api" and (groq.pricing.input, groq.pricing.output) == (0.15, 0.60)
    opus = reg.specs[OPUS]
    assert (opus.pricing.input, opus.pricing.output, opus.pricing.cache_read) == (4.0, 20.0, 0.2)
    for local in ("ollama:qwen3.5:9b", "ollama:gemma4:e4b", "ollama:granite4:tiny-h"):
        assert reg.specs[local].kind == "local" and reg.specs[local].pricing.input == 0
    assert {"fake:agent", "fake:secondary", "fake:primary_503", "baseline:deterministic"} <= set(reg.specs)


def test_anthropic_param_rules() -> None:
    settings = fixture_settings()
    spec = get_spec(OPUS)
    agent = provider_kwargs(spec, "agent", settings)
    parse = provider_kwargs(spec, "parse", settings)
    for kwargs in (agent, parse):
        assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(kwargs)
        assert kwargs["max_tokens"] == 16000
        assert "server-side-fallback-2026-07-01" in kwargs["betas"]
        assert kwargs["model_kwargs"] == {"fallbacks": "default"}
    assert agent["output_config"] == {"effort": "medium"} and parse["output_config"] == {"effort": "low"}


def test_anthropic_model_builds_a_valid_request_payload() -> None:
    settings = fixture_settings()
    model = get_chat_model(OPUS, "agent", settings)
    assert isinstance(model, ChatAnthropic)
    assert model.temperature is None and model.top_p is None and model.top_k is None
    bound = model.bind_tools(tool_schemas())
    assert "tool_choice" not in bound.kwargs  # type: ignore[attr-defined]
    prepared = registry._prepare_messages(get_spec(OPUS))([SystemMessage(content=AGENT_SYSTEM_PROMPT),
                                                            HumanMessage(content="{}")])
    payload = model._get_request_payload(prepared, **bound.kwargs)  # type: ignore[attr-defined]
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["system"][0]["text"] == AGENT_SYSTEM_PROMPT
    assert payload["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in payload["betas"]
    assert payload["output_config"]["effort"] == "medium"
    assert payload["max_tokens"] == 16000
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(payload)
    assert [t["name"] for t in payload["tools"]] == ["search_places", "search_events", "estimate_travel",
                                                       "check_plan", "submit_plan"]


def test_ollama_param_rules() -> None:
    settings = fixture_settings(ollama_base_url="http://127.0.0.1:11999")
    qwen = get_chat_model("ollama:qwen3.5:9b", "agent", settings)
    assert isinstance(qwen, ChatOllama)
    assert qwen.num_ctx == 32768 and qwen.temperature == 0 and qwen.keep_alive == "10m"
    assert qwen.reasoning is False and qwen.base_url == "http://127.0.0.1:11999"
    granite = get_chat_model("ollama:granite4:tiny-h", "agent", settings)
    assert isinstance(granite, ChatOllama) and granite.num_ctx == 32768 and granite.reasoning is None
    adhoc = get_spec("ollama:llama3.2:3b")
    assert adhoc.kind == "local" and provider_kwargs(adhoc, "agent", settings)["num_ctx"] == 32768


def test_pending_unknown_and_baseline_ids_raise_clearly() -> None:
    settings = fixture_settings()
    with pytest.raises(PendingModelError, match=r"models\.yaml"):
        get_spec("xai:<pending>")
    with pytest.raises(PendingModelError, match="No xai model ID is pinned"):
        get_chat_model("xai:grok-9", "agent", settings)
    with pytest.raises(UnknownModelError):
        get_spec("google_genai:gemini-9-pro")  # Gemini IDs are pinned; anything else is unknown
    with pytest.raises(UnknownModelError, match=r"Known models"):
        get_spec("openai:gpt-9")
    with pytest.raises(UnknownModelError):
        get_spec("no-provider")
    with pytest.raises(RegistryError, match="no-LLM baseline"):
        get_chat_model("baseline:deterministic", "agent", settings)


def test_model_chain_and_fallbacks() -> None:
    settings = fixture_settings(primary_model="fake:agent", fallback_model="fake:secondary")
    assert model_chain(settings) == ["fake:agent", "fake:secondary"]
    assert model_chain(settings, "fake:secondary") == ["fake:secondary"]
    assert model_chain(settings, "baseline:deterministic") == ["baseline:deterministic"]
    assert model_chain(fixture_settings(primary_model="fake:agent")) == ["fake:agent"]
    runnable = build_agent_runnable(model_chain(settings), tool_schemas(), settings)
    assert isinstance(runnable, Runnable)
    structured = build_structured_runnable(model_chain(settings), LLMParse, settings)
    assert isinstance(structured, Runnable)


def test_models_are_cached_per_purpose_and_fault() -> None:
    settings = fixture_settings(fault="llm_refusal")
    a = get_chat_model("fake:agent", "agent", settings)
    assert get_chat_model("fake:agent", "agent", settings) is a
    assert get_chat_model("fake:agent", "parse", settings) is not a
    assert isinstance(a, ScriptedAgentModel) and a.fault == "llm_refusal"


def test_is_refusal() -> None:
    assert is_refusal(AIMessage(content="", response_metadata={"stop_reason": "refusal"}))
    assert is_refusal(AIMessage(content="", response_metadata={"finish_reason": "SAFETY"}))
    assert is_refusal(AIMessage(content="", additional_kwargs={"refusal": "no"}))
    assert not is_refusal(AIMessage(content="ok", response_metadata={"stop_reason": "end_turn"}))


async def test_refusal_moves_down_the_chain_and_raises_at_the_end() -> None:
    settings = fixture_settings(primary_model="fake:agent", fault="llm_refusal")
    runnable = build_agent_runnable(["fake:agent"], tool_schemas(), settings)
    with pytest.raises(ModelRefusalError):
        await runnable.ainvoke([HumanMessage(content="{}")])


async def test_fallback_answer_is_tagged_with_the_model_that_ran() -> None:
    settings = fixture_settings(primary_model="fake:primary_503", fallback_model="fake:secondary")
    runnable = build_agent_runnable(model_chain(settings), tool_schemas(), settings)
    message = await runnable.ainvoke([HumanMessage(content="{}")])
    assert message.response_metadata[METADATA_KEY] == "fake:secondary"


async def test_structured_output_written_as_text_is_recovered() -> None:
    from langchain_core.runnables import RunnableLambda

    raw = AIMessage(content='Sure:\n{"city": "Pune", "energy": "low"}')
    tag = registry._tag_structured("ollama:qwen3.5:9b", LLMParse)
    out = await (RunnableLambda(lambda _: {"raw": raw, "parsed": None, "parsing_error": None}) | tag).ainvoke(None)
    assert out["parsed"].city == "Pune" and out["model_id"] == "ollama:qwen3.5:9b"
    with pytest.raises(registry.StructuredOutputError):
        tag({"raw": AIMessage(content="no json here"), "parsed": None, "parsing_error": None})


def test_every_ollama_row_caps_output_tokens() -> None:
    settings = fixture_settings()
    for model_id in ("ollama:qwen3.5:9b", "ollama:gemma4:e4b", "ollama:granite4:tiny-h", "ollama:llama3.2:3b"):
        model = get_chat_model(model_id, "agent", settings)
        assert isinstance(model, ChatOllama) and model.num_predict == 2048, model_id


def test_groq_param_rules() -> None:
    settings = fixture_settings()
    spec = get_spec("groq:openai/gpt-oss-120b")
    agent = provider_kwargs(spec, "agent", settings)
    parse = provider_kwargs(spec, "parse", settings)
    assert agent["reasoning_effort"] == "medium" and parse["reasoning_effort"] == "low"
    assert agent["reasoning_format"] == "hidden" and agent["temperature"] == 0.0
    assert agent["max_tokens"] == 8192 and agent["request_timeout"] == 60.0 and agent["max_retries"] == 0


def test_gemini_keeps_provider_default_temperature() -> None:
    kwargs = provider_kwargs(get_spec("google_genai:gemini-3.6-flash"), "agent", fixture_settings())
    assert "temperature" not in kwargs and kwargs["max_output_tokens"] == 8192 and kwargs["timeout"] == 60.0


def test_cerebras_param_rules_and_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-test-not-a-real-key")
    settings = fixture_settings()
    spec = get_spec("cerebras:gpt-oss-120b")
    kwargs = provider_kwargs(spec, "agent", settings)
    assert kwargs["reasoning_effort"] == "medium" and provider_kwargs(spec, "parse", settings)["reasoning_effort"] == "low"
    assert kwargs["temperature"] == 0.0 and kwargs["max_tokens"] == 8192 and kwargs["request_timeout"] == 60.0
    from langchain_cerebras import ChatCerebras

    model = get_chat_model("cerebras:gpt-oss-120b", "agent", settings)  # built directly, not via init_chat_model
    assert isinstance(model, ChatCerebras) and model.model_name == "gpt-oss-120b"


def test_production_chain_retry_settings_and_structured_schema() -> None:
    settings = fixture_settings()
    cerebras = get_spec("cerebras:gpt-oss-120b")
    groq = get_spec("groq:openai/gpt-oss-120b")
    assert provider_kwargs(cerebras, "agent", settings)["max_retries"] == 0  # a 429 goes straight to Groq
    assert provider_kwargs(groq, "agent", settings)["max_retries"] == 0
    as_dict = registry.structured_schema(cerebras, LLMParse)
    assert isinstance(as_dict, dict) and as_dict["title"] == "LLMParse"
    assert "$defs" not in json.dumps(as_dict) and "$ref" not in json.dumps(as_dict)
    assert registry.structured_schema(groq, LLMParse) is LLMParse


async def test_cerebras_structured_parse_has_no_serializer_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    import warnings

    import httpx
    from langchain_cerebras import ChatCerebras

    from app.domain.schemas import PlanRequest
    from app.graph.prompts import parse_messages
    from tests.factories import NOW

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"city": "Pune", "energy": "low", "hard_constraints": [{"kind": "diet", "value": "vegan"}]})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 1, "model": "gpt-oss-120b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    llm = ChatCerebras(model="gpt-oss-120b", api_key="test-key",  # type: ignore[arg-type]
                       http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(registry, "get_chat_model", lambda *a, **k: llm)
    runnable = build_structured_runnable(["cerebras:gpt-oss-120b"], LLMParse, fixture_settings())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = await runnable.ainvoke(parse_messages(PlanRequest(free_text="Pune, vegan"), NOW))
    assert isinstance(out["parsed"], LLMParse) and out["parsed"].hard_constraints[0].value == "vegan"
    assert not [w for w in caught if "PydanticSerializationUnexpectedValue" in str(w.message)]


def test_per_model_agent_turn_cap() -> None:
    from app.llm.registry import agent_turn_cap

    assert agent_turn_cap(fixture_settings(), "cerebras:gpt-oss-120b") == 4
    assert agent_turn_cap(fixture_settings(max_agent_turns=3), "cerebras:gpt-oss-120b") == 3
    assert agent_turn_cap(fixture_settings(), "groq:openai/gpt-oss-120b") == 10
    assert agent_turn_cap(fixture_settings(), "fake:agent") == 10


def test_render_blueprint_uses_the_registered_production_model_without_hosted_fallback() -> None:
    import yaml

    root = Path(__file__).resolve().parents[3]
    service = yaml.safe_load((root / "render.yaml").read_text())["services"][0]
    values = {item["key"]: item.get("value") for item in service["envVars"]}
    reg = load_registry()
    assert values["PRIMARY_MODEL"] == reg.production_primary
    assert values["FALLBACK_MODEL"] == ""
    assert values["APP_ENV"] == "prod"
    assert "GROQ_API_KEY" in values
    settings = fixture_settings(primary_model=values["PRIMARY_MODEL"], fallback_model=values["FALLBACK_MODEL"])
    assert model_chain(settings) == ["groq:openai/gpt-oss-120b"]
