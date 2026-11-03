from types import SimpleNamespace

import pytest

from app.llm.provider_errors import provider_error_details


@pytest.mark.parametrize(("header", "expected"), [("30", 30), ("0.5", 0.5), ("-1", None),
                                                  ("nan", None), ("inf", None), ("invalid", None)])
def test_provider_metadata_is_safe_and_retry_delay_is_bounded_data(header, expected):
    exc = RuntimeError("secret response body and API credential")
    exc.response = SimpleNamespace(status_code=429, headers={"retry-after": header})
    details = provider_error_details(exc)
    assert details["http_status"] == 429
    assert details.get("retry_after_s") == expected
    assert "secret" not in str(details) and "credential" not in str(details)


async def test_groq_429_does_not_trigger_hidden_sdk_sleep(monkeypatch):
    import asyncio

    import httpx
    import respx
    from groq import RateLimitError
    from langchain_core.messages import HumanMessage

    from app.deps import fixture_settings
    from app.llm import registry
    from app.llm.ratelimit import reset_limiters

    monkeypatch.setenv("GROQ_API_KEY", "test-not-a-real-key")
    registry.clear_cache()
    reset_limiters()
    model = registry.get_chat_model("groq:openai/gpt-oss-120b", "agent", fixture_settings())
    with respx.mock:
        route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(return_value=httpx.Response(
            429, headers={"retry-after": "30"}, json={"error": {"message": "Rate limit", "type": "tokens"}}))
        with pytest.raises(RateLimitError):
            await asyncio.wait_for(model.ainvoke([HumanMessage(content="test")]), 2)
        assert route.call_count == 1
    registry.clear_cache()
    reset_limiters()
