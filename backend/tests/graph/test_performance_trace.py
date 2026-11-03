from types import SimpleNamespace

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableLambda

from app.domain.schemas import PlanRequest
from app.graph.nodes import agent
from app.graph.runner import stream_plan
from tests.graph.helpers import make_deps


async def test_provider_retry_after_is_visible_without_exposing_body(monkeypatch):
    original = agent.build_agent_runnable

    class RateLimitFailure(Exception):
        status_code = 429
        response = SimpleNamespace(status_code=429, headers={"retry-after": "30"})

    def build(*args, **kwargs):
        inner = original(*args, **kwargs)

        async def invoke(value, config):
            if any(isinstance(m, ToolMessage) for m in value):
                raise RateLimitFailure("secret provider body")
            return await inner.ainvoke(value, config=config)

        return RunnableLambda(invoke)

    monkeypatch.setattr(agent, "build_agent_runnable", build)
    deps = make_deps()
    events = [e async for e in stream_plan(PlanRequest(city="Bangalore", budget=2000, available_time="4 hours",
                                                     interests=["coffee", "museums"]), deps)]
    notices = [e for e in events if e.type == "degraded" and e.data and e.data.get("http_status") == 429]
    assert len(notices) == 1 and notices[0].data["retry_after_s"] == 30
    assert "instead of waiting 30 seconds" in notices[0].summary
    assert "secret provider body" not in str([e.model_dump() for e in events])
    final = [e for e in events if e.type == "result"][-1].data
    assert final["mode"] == "deterministic" and final["status"] == "fallback"
    assert any("rate limit" in d for d in final["degradations"])
