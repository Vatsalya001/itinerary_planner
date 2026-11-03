"""Token usage and cost accounting per run.

``UsageTracker`` is a LangChain callback handler: pass it in the ``callbacks`` of every model call. It counts
every LLM call (including failed ones), sums ``usage_metadata`` from the returned AIMessages and prices
them with the registry pricing of the model that answered. The model is identified by the
``registry_model_id`` metadata that app.llm.registry attaches to each chain.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.llm.pricing import pricing_for
from app.llm.ratelimit import RateLimitWaitError

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cost_usd", "llm_calls", "rate_limit_wait_ms")


def empty_usage() -> dict[str, Any]:
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0, "llm_calls": 0,
            "rate_limit_wait_ms": 0}


def add_usage(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    """Reducer: element-wise sum of two usage dicts (cost rounded to 6 decimals)."""
    out = empty_usage()
    for src in (a or {}, b or {}):
        for key in USAGE_KEYS:
            out[key] += src.get(key, 0) or 0
    out["cost_usd"] = round(float(out["cost_usd"]), 6)
    return out


def message_usage(message: AIMessage, model_id: str | None) -> dict[str, Any]:
    """Usage dict for one AIMessage (zeros when the provider reported nothing)."""
    meta: dict[str, Any] = dict(message.usage_metadata or {})
    input_tokens = int(meta.get("input_tokens", 0) or 0)
    output_tokens = int(meta.get("output_tokens", 0) or 0)
    details = meta.get("input_token_details") or {}
    cache_read = int(details.get("cache_read", 0) or 0)
    cache_write = int(details.get("cache_creation", 0) or 0)
    cost = pricing_for(model_id).cost_usd(input_tokens, output_tokens, cache_read, cache_write)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_tokens": cache_read,
            "cost_usd": round(cost, 6), "llm_calls": 0}


class UsageTracker(BaseCallbackHandler):
    """Collects usage for the model calls made while it is attached."""

    run_inline = True  # keep accounting on the event loop thread
    raise_error = False

    def __init__(self) -> None:
        self.usage: dict[str, Any] = empty_usage()
        self.models: list[str] = []
        self._run_models: dict[UUID, str | None] = {}

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[BaseMessage]], *,
                            run_id: UUID, parent_run_id: UUID | None = None, tags: list[str] | None = None,
                            metadata: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self._run_models[run_id] = (metadata or {}).get("registry_model_id")
        self.usage = add_usage(self.usage, {"llm_calls": 1})

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, parent_run_id: UUID | None = None,
                   **kwargs: Any) -> Any:
        model_id = self._run_models.pop(run_id, None)
        delta = empty_usage()
        delta["llm_calls"] = 0
        for generations in response.generations:
            for gen in generations:
                if isinstance(gen, ChatGeneration) and isinstance(gen.message, AIMessage):
                    delta = add_usage(delta, message_usage(gen.message, model_id))
        self.usage = add_usage(self.usage, delta)
        if model_id:
            self.models.append(model_id)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, parent_run_id: UUID | None = None,
                     **kwargs: Any) -> Any:
        self._run_models.pop(run_id, None)
        if isinstance(error, RateLimitWaitError):
            self.usage["llm_calls"] = max(0, self.usage["llm_calls"] - 1)

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        if isinstance(error, TimeoutError | asyncio.CancelledError):
            self._run_models.clear()
