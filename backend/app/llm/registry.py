"""Provider-agnostic model registry (IMPLEMENTATION_PLAN.md section 8).

``models.yaml`` lists every model the app may run. ``get_chat_model`` builds one LangChain chat model via
``init_chat_model("provider:model")`` with the provider-specific rules below; ``build_agent_runnable`` and
``build_structured_runnable`` wrap a model chain (primary, then ``settings.fallback_model``) with
``with_fallbacks``, tag each response with the registry ID of the model that answered, and turn refusals
into exceptions so the next model in the chain (and finally the deterministic planner) takes over.

Provider rules:

* Anthropic (Claude Opus 5.5): no temperature/top_p/top_k (rejected with 400); no forced tool_choice;
  structured output via native ``json_schema``; thinking is always on, depth via ``output_config.effort``
  per purpose; ``max_tokens`` 16000; server-side refusal fallback via the beta header plus the request
  field ``fallbacks: "default"`` (passed through ``model_kwargs``, routed to ``client.beta.messages``);
  the static system prompt is sent as a text block with ``cache_control: {"type": "ephemeral"}``.
* Ollama: ``num_ctx`` is always set (the default context truncates tool definitions silently),
  temperature 0, ``keep_alive``, ``reasoning`` False unless the registry says otherwise.
* fake: the scripted offline model (app.llm.fake). baseline: no chat model at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda

from app.config import Settings
from app.llm.pricing import Pricing

log = logging.getLogger("app.llm")

Purpose = Literal["parse", "agent", "judge"]
Role = Literal["primary", "fallback"]
Kind = Literal["api", "local", "fake", "baseline"]

REGISTRY_PATH = Path(__file__).with_name("models.yaml")
BASELINE_ID = "baseline:deterministic"
METADATA_KEY = "registry_model_id"
_SAMPLING_PARAMS = ("temperature", "top_p", "top_k")
DEFAULT_NUM_CTX = 32768
DEFAULT_NUM_PREDICT = 2048  # Ollama output cap per call (a runaway generation otherwise eats the whole turn)


class RegistryError(ValueError):
    """The model ID can't be built (unknown, pending, or not a chat model)."""


class UnknownModelError(RegistryError):
    pass


class PendingModelError(RegistryError):
    pass


class ModelRefusalError(RuntimeError):
    """The model declined (stop_reason "refusal"). Treated as a model failure."""

    def __init__(self, model_id: str) -> None:
        super().__init__(f"{model_id} refused the request")
        self.model_id = model_id


class StructuredOutputError(RuntimeError):
    """Structured output could not be parsed into the schema."""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    model: str
    kind: Kind
    params: Mapping[str, Any] = field(default_factory=dict)
    pricing: Pricing = field(default_factory=Pricing)
    pending: bool = False
    note: str = ""
    agent_turn_s: float = 45.0
    parse_s: float = 15.0


@dataclass(frozen=True)
class Registry:
    specs: dict[str, ModelSpec]
    production_primary: str
    production_fallback: str | None
    timeouts: dict[str, dict[str, float]]


def split_model_id(model_id: str) -> tuple[str, str]:
    """"ollama:qwen3.5:9b" -> ("ollama", "qwen3.5:9b")."""
    provider, sep, model = model_id.partition(":")
    if not sep or not provider or not model:
        raise UnknownModelError(f"Model ID '{model_id}' must look like 'provider:model'")
    return provider, model


@lru_cache(maxsize=4)
def load_registry(path: str | None = None) -> Registry:
    raw = yaml.safe_load(Path(path or REGISTRY_PATH).read_text(encoding="utf-8"))
    timeouts = {k: {kk: float(vv) for kk, vv in v.items()} for k, v in (raw.get("timeouts") or {}).items()}
    specs: dict[str, ModelSpec] = {}
    for entry in raw.get("candidates", []):
        model_id = str(entry["id"])
        provider, model = split_model_id(model_id)
        kind = cast(Kind, entry.get("kind", "api"))
        t = timeouts.get(kind, {})
        specs[model_id] = ModelSpec(
            id=model_id, provider=provider, model=model, kind=kind, params=dict(entry.get("params") or {}),
            pricing=Pricing.from_dict(entry.get("pricing_per_mtok")), pending=bool(entry.get("pending", False)),
            note=str(entry.get("note", "")), agent_turn_s=t.get("agent_turn_s", 45.0), parse_s=t.get("parse_s", 15.0),
        )
    production = raw.get("production") or {}
    return Registry(specs=specs, production_primary=str(production.get("primary", "")),
                    production_fallback=production.get("fallback"), timeouts=timeouts)


def find_spec(model_id: str) -> ModelSpec | None:
    """The registry entry, an ad-hoc local Ollama entry, or None."""
    registry = load_registry()
    if model_id in registry.specs:
        return registry.specs[model_id]
    try:
        provider, model = split_model_id(model_id)
    except UnknownModelError:
        return None
    if provider == "ollama":  # any local model works for dev with the safe local defaults
        t = registry.timeouts.get("local", {})
        return ModelSpec(id=model_id, provider="ollama", model=model, kind="local",
                         params={"num_ctx": DEFAULT_NUM_CTX, "num_predict": DEFAULT_NUM_PREDICT, "temperature": 0,
                                 "keep_alive": "10m", "reasoning": False},
                         note="ad-hoc local model (not in models.yaml)", agent_turn_s=t.get("agent_turn_s", 150.0),
                         parse_s=t.get("parse_s", 60.0))
    return None


def get_spec(model_id: str) -> ModelSpec:
    """Registry entry for ``model_id``. Raises PendingModelError for placeholders (and for any model of a
    provider whose only entry is still pending), UnknownModelError for everything else not listed."""
    spec = find_spec(model_id)
    if spec is not None and not spec.pending:
        return spec
    provider = model_id.partition(":")[0]
    pending = [s for s in load_registry().specs.values() if s.provider == provider and s.pending]
    if spec is not None or pending:
        entry = spec or pending[0]
        raise PendingModelError(
            f"No {provider} model ID is pinned yet ('{model_id}'). {entry.note} "
            f"Edit app/llm/models.yaml to replace '{entry.id}' with a real model ID.".strip())
    known = ", ".join(sorted(s for s, v in load_registry().specs.items() if not v.pending))
    raise UnknownModelError(f"Model '{model_id}' is not in app/llm/models.yaml. Known models: {known}")


def is_baseline(model_id: str | None) -> bool:
    return model_id == BASELINE_ID


def model_chain(settings: Settings, model_id: str | None = None) -> list[str]:
    """[primary] or [primary, fallback] for a run. The fallback is skipped when it equals the primary."""
    primary = model_id or settings.primary_model
    chain = [primary]
    fallback = settings.fallback_model
    if fallback and fallback != primary and not is_baseline(primary) and not is_baseline(fallback):
        chain.append(fallback)
    return chain


# ---------------------------------------------------------------------------------------------------
# Provider parameters
# ---------------------------------------------------------------------------------------------------


def provider_kwargs(spec: ModelSpec, purpose: Purpose, settings: Settings) -> dict[str, Any]:
    """Constructor kwargs for ``init_chat_model`` (pure; unit-tested per provider)."""
    p = dict(spec.params)
    if spec.provider == "anthropic":
        efforts = p.get("effort") or {}
        effort = efforts.get(purpose) if isinstance(efforts, Mapping) else efforts
        kwargs: dict[str, Any] = {
            "max_tokens": int(p.get("max_tokens", 16000)),
            "max_retries": int(p.get("max_retries", 1)),
            "timeout": float(p.get("request_timeout_s", 60)),
        }
        if effort:
            kwargs["output_config"] = {"effort": effort}
        betas = list(p.get("betas") or [])
        fallback_mode = p.get("server_side_fallback")
        if fallback_mode:
            if "server-side-fallback-2026-07-01" not in betas:
                betas.append("server-side-fallback-2026-07-01")
            kwargs["model_kwargs"] = {"fallbacks": fallback_mode}
        if betas:
            kwargs["betas"] = betas
        dropped = [k for k in _SAMPLING_PARAMS if k in p]
        if dropped:
            log.warning("ignoring sampling params for %s (rejected by the API): %s", spec.id, dropped)
        return kwargs
    if spec.provider == "ollama":
        kwargs = {
            "base_url": settings.ollama_base_url,
            "num_ctx": int(p.get("num_ctx") or DEFAULT_NUM_CTX),
            "temperature": float(p.get("temperature", 0)),
            "keep_alive": p.get("keep_alive", "10m"),
            "validate_model_on_init": False,
        }
        reasoning = p.get("reasoning", False)
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        kwargs["num_predict"] = int(p.get("num_predict") or DEFAULT_NUM_PREDICT)
        return kwargs
    if spec.provider == "google_genai":
        kwargs = {"max_retries": int(p.get("max_retries", 1)), "timeout": float(p.get("request_timeout_s", 60))}
        # Gemini 3+ is tuned for the default temperature; set it only when the registry entry asks for one.
        if p.get("temperature") is not None:
            kwargs["temperature"] = float(p["temperature"])
        if p.get("max_tokens"):
            kwargs["max_output_tokens"] = int(p["max_tokens"])
        budgets = p.get("thinking_budget")
        budget = budgets.get(purpose) if isinstance(budgets, Mapping) else budgets
        if budget is not None:
            kwargs["thinking_budget"] = int(budget)
        return kwargs
    if spec.provider == "groq":
        kwargs = {
            "temperature": float(p.get("temperature", 0)),
            "max_retries": int(p.get("max_retries", 1)),
            "request_timeout": float(p.get("request_timeout_s", 60)),
        }
        if p.get("max_tokens"):
            kwargs["max_tokens"] = int(p["max_tokens"])
        efforts = p.get("reasoning_effort")
        effort = efforts.get(purpose) if isinstance(efforts, Mapping) else efforts
        if effort:
            kwargs["reasoning_effort"] = effort
        if p.get("reasoning_format"):
            kwargs["reasoning_format"] = p["reasoning_format"]
        return kwargs
    if spec.provider == "xai":
        kwargs = {"temperature": float(p.get("temperature", 0)), "max_retries": int(p.get("max_retries", 1))}
        if p.get("max_tokens"):
            kwargs["max_tokens"] = int(p["max_tokens"])
        return kwargs
    return {k: v for k, v in p.items() if k not in {"effort", "cache_system_prompt", "structured_output"}}


_MODEL_CACHE: dict[tuple[Any, ...], BaseChatModel] = {}


def get_chat_model(model_id: str, purpose: Purpose, settings: Settings, *, role: Role = "primary") -> BaseChatModel:
    """One chat model for ``model_id``. Raises RegistryError subclasses for unknown/pending/baseline IDs."""
    spec = get_spec(model_id)
    if spec.kind == "baseline":
        raise RegistryError(f"{model_id} is the no-LLM baseline and has no chat model")
    fault = settings.active_fault()
    key = (spec.id, purpose, role, fault, settings.ollama_base_url)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    model: BaseChatModel
    if spec.provider == "fake":
        from app.llm.fake import ScriptedAgentModel

        model = ScriptedAgentModel(model_id=spec.id, fault=fault, role=role)
    else:
        from langchain.chat_models import init_chat_model

        model = init_chat_model(spec.model, model_provider=spec.provider, **provider_kwargs(spec, purpose, settings))
    if len(_MODEL_CACHE) > 64:
        _MODEL_CACHE.clear()
    _MODEL_CACHE[key] = model
    return model


# ---------------------------------------------------------------------------------------------------
# Chains: message adaptation, refusal detection, tagging, fallbacks
# ---------------------------------------------------------------------------------------------------


def structured_method(spec: ModelSpec) -> Literal["json_schema", "function_calling"]:
    """Native JSON-schema output by default (required for Claude: forced tool choice is rejected); local models
    whose runtime lacks schema-constrained output use tool-call based structured output instead."""
    if spec.provider == "anthropic":
        return "json_schema"
    return "function_calling" if spec.params.get("structured_output") == "function_calling" else "json_schema"


def is_refusal(message: BaseMessage) -> bool:
    meta = message.response_metadata or {}
    if meta.get("stop_reason") == "refusal":
        return True
    if str(meta.get("finish_reason", "")).lower() in {"refusal", "safety", "content_filter"}:
        return True
    return bool((message.additional_kwargs or {}).get("refusal"))


def _system_text(message: SystemMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in message.content)


def _prepare_messages(spec: ModelSpec) -> Callable[[Sequence[BaseMessage]], list[BaseMessage]]:
    """Provider-specific message adaptation. Anthropic gets the static system prompt as a cacheable block;
    every other provider gets plain-string system content."""
    cache = spec.provider == "anthropic" and bool(spec.params.get("cache_system_prompt", True))

    def prepare(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        out: list[BaseMessage] = []
        for m in messages:
            if isinstance(m, SystemMessage):
                text = _system_text(m)
                if cache:
                    m = SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])
                elif not isinstance(m.content, str):
                    m = SystemMessage(content=text)
            out.append(m)
        return out

    return prepare


def _tag_agent(model_id: str) -> Callable[[AIMessage], AIMessage]:
    def tag(message: AIMessage) -> AIMessage:
        if is_refusal(message):
            raise ModelRefusalError(model_id)
        meta = dict(message.response_metadata or {})
        meta[METADATA_KEY] = model_id
        return message.model_copy(update={"response_metadata": meta})

    return tag


def _json_from_text(raw: BaseMessage, schema: type[Any]) -> Any:
    """Recover a structured answer that a local model wrote as text instead of as the requested tool call.
    The text must hold one JSON object that validates against the same schema; anything else stays None."""
    text = raw.content if isinstance(raw.content, str) else ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start or not hasattr(schema, "model_validate_json"):
        return None
    try:
        return schema.model_validate_json(text[start:end + 1])
    except ValueError:
        return None


def _tag_structured(model_id: str, schema: type[Any]) -> Callable[[Any], dict[str, Any]]:
    def tag(output: Any) -> dict[str, Any]:
        if not isinstance(output, dict):
            raise StructuredOutputError(f"{model_id}: expected include_raw output")
        raw = output.get("raw")
        if isinstance(raw, BaseMessage) and is_refusal(raw):
            raise ModelRefusalError(model_id)
        parsed = output.get("parsed")
        if parsed is None and isinstance(raw, BaseMessage):
            parsed = _json_from_text(raw, schema)
        if parsed is None:
            error = output.get("parsing_error")
            raise StructuredOutputError(f"{model_id}: {type(error).__name__ if error else 'empty output'}")
        return {"parsed": parsed, "model_id": model_id}

    return tag


def _agent_chain(model_id: str, purpose: Purpose, settings: Settings, role: Role,
                 tools: Sequence[dict[str, Any]]) -> Runnable[Any, AIMessage]:
    spec = get_spec(model_id)
    model = get_chat_model(model_id, purpose, settings, role=role)
    bound = model.bind_tools(list(tools))  # tool_choice left to the model ("auto"): forced choice 400s on Claude
    return (RunnableLambda(_prepare_messages(spec))
            | bound.with_config(metadata={METADATA_KEY: model_id})
            | RunnableLambda(_tag_agent(model_id)))


def with_timeout(chain: Runnable[Any, Any], seconds: float) -> Runnable[Any, Any]:
    """Bound one model's attempt so a hung primary raises TimeoutError inside ``with_fallbacks`` and the
    next model in the chain still gets a turn."""

    async def run(value: Any, config: RunnableConfig) -> Any:
        return await asyncio.wait_for(chain.ainvoke(value, config=config), seconds)

    return RunnableLambda(run)


def _roles(model_ids: Sequence[str], primary_id: str | None) -> list[Role]:
    """The configured primary keeps the "primary" role even when a run has already switched away from it."""
    primary = primary_id or model_ids[0]
    return ["primary" if mid == primary else "fallback" for mid in model_ids]


def build_agent_runnable(model_ids: Sequence[str], tools: Sequence[dict[str, Any]], settings: Settings,
                         purpose: Purpose = "agent", *, primary_timeout_s: float | None = None,
                         primary_id: str | None = None) -> Runnable[Any, AIMessage]:
    """Tool-calling runnable over the chain; each AIMessage carries ``response_metadata[METADATA_KEY]``.
    ``primary_timeout_s`` caps the first model's attempt when a fallback model follows it."""
    if not model_ids:
        raise RegistryError("empty model chain")
    roles = _roles(model_ids, primary_id)
    chains = [_agent_chain(mid, purpose, settings, role, tools) for mid, role in zip(model_ids, roles, strict=True)]
    if len(chains) == 1:
        return chains[0]
    first = with_timeout(chains[0], primary_timeout_s) if primary_timeout_s else chains[0]
    return first.with_fallbacks(chains[1:])


def build_structured_runnable(model_ids: Sequence[str], schema: type[Any], settings: Settings,
                              purpose: Purpose = "parse", *, primary_timeout_s: float | None = None,
                              primary_id: str | None = None) -> Runnable[Any, dict[str, Any]]:
    """Structured-output runnable returning ``{"parsed": schema instance, "model_id": ...}``."""
    if not model_ids:
        raise RegistryError("empty model chain")
    chains: list[Runnable[Any, dict[str, Any]]] = []
    roles = _roles(model_ids, primary_id)
    for model_id, role in zip(model_ids, roles, strict=True):
        spec = get_spec(model_id)
        model = get_chat_model(model_id, purpose, settings, role=role)
        structured = model.with_structured_output(schema, method=structured_method(spec), include_raw=True)
        chains.append(RunnableLambda(_prepare_messages(spec))
                      | structured.with_config(metadata={METADATA_KEY: model_id})
                      | RunnableLambda(_tag_structured(model_id, schema)))
    if len(chains) == 1:
        return chains[0]
    first = with_timeout(chains[0], primary_timeout_s) if primary_timeout_s else chains[0]
    return first.with_fallbacks(chains[1:])


def turn_timeout_s(model_id: str) -> float:
    spec = find_spec(model_id)
    return spec.agent_turn_s if spec is not None else 45.0


def parse_timeout_s(model_id: str) -> float:
    spec = find_spec(model_id)
    return spec.parse_s if spec is not None else 15.0


def clear_cache() -> None:
    _MODEL_CACHE.clear()
