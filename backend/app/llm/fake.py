"""Scripted, deterministic chat model for offline tests and the ``offline`` eval suite.

``ScriptedAgentModel`` is a real LangChain ``BaseChatModel``: ``bind_tools`` returns a bound copy and
``with_structured_output`` returns a runnable, so it runs through exactly the same registry chains,
callbacks and graph nodes as a hosted model. It reads everything it needs from the JSON payloads defined
in app.graph.prompts and from ToolMessage artifacts; it never sees the real objects.

Policy (agent mode):
  turn 1   search_places for each default query (max 6) + search_events
  turn 2   compose with the deterministic planner over the returned candidates, then estimate_travel +
           check_plan on that draft (or a one-sentence text reply when nothing fits)
  turn 3+  submit_plan; after a rejected submission, re-compose and submit again

Faults (``settings.fault``): ``llm_timeout`` (never answers), ``llm_refusal`` (stop_reason "refusal"),
``primary_provider_503`` (primary role raises an HTTP-503-like error; ``fake:primary_503`` always does),
``submit_plan_unknown_ids_twice`` (every submission uses invented IDs), ``text_only_final_turn`` (answers in
text instead of submitting, every time), ``parser_omits_diet_and_agent_selects_nonveg`` (the parse drops
diet constraints and the first submission swaps in a non-vegetarian food stop when one is in the ledger).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

FAKE_IDS = ("fake:agent", "fake:secondary", "fake:primary_503")
HANG_SECONDS = 3600.0


class FakeProviderHTTPError(Exception):
    """Looks like an HTTP 503 from a provider SDK (status_code attribute, 'Service Unavailable' text)."""

    def __init__(self, model_id: str) -> None:
        super().__init__(f"Error code: 503 - Service Unavailable ({model_id})")
        self.status_code = 503


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    return sum(len(str(m.content)) for m in messages) // 4 + 1


class ScriptedAgentModel(BaseChatModel):
    model_id: str = "fake:agent"
    fault: str | None = None
    role: str = "primary"
    mode: str = "chat"  # chat | agent | parse
    tool_names: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "fault": self.fault, "role": self.role, "mode": self.mode}

    # -- LangChain surface --------------------------------------------------------------------------

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: str | None = None,
                   **kwargs: Any) -> Runnable[LanguageModelInput, AIMessage]:
        names: list[str] = []
        for tool in tools:
            if isinstance(tool, dict):
                fn = tool.get("function", tool)
                names.append(str(fn.get("name")))
            else:
                names.append(str(getattr(tool, "name", type(tool).__name__)))
        return self.model_copy(update={"mode": "agent", "tool_names": tuple(names)})

    def with_structured_output(self, schema: Any, *, include_raw: bool = False,
                               method: str = "json_schema", **kwargs: Any) -> Runnable[LanguageModelInput, Any]:
        def to_structured(message: AIMessage) -> Any:
            parsed: Any = None
            error: Exception | None = None
            try:
                parsed = schema.model_validate_json(str(message.content))
            except Exception as exc:  # mirrors LangChain's include_raw behaviour
                error = exc
            if include_raw:
                return {"raw": message, "parsed": parsed, "parsing_error": error}
            if error is not None:
                raise error
            return parsed

        return self.model_copy(update={"mode": "parse"}) | RunnableLambda(to_structured)

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        if self._hangs():
            raise TimeoutError(f"{self.model_id} timed out")
        return self._result(messages)

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        if self._hangs():
            await asyncio.sleep(HANG_SECONDS)  # cancelled by the caller's deadline
            raise TimeoutError(f"{self.model_id} timed out")
        return self._result(messages)

    # -- behaviour ----------------------------------------------------------------------------------

    def _hangs(self) -> bool:
        return self.fault == "llm_timeout" and self.mode == "agent"

    def _fails_like_503(self) -> bool:
        return self.model_id == "fake:primary_503" or (self.fault == "primary_provider_503" and self.role == "primary")

    def _result(self, messages: list[BaseMessage]) -> ChatResult:
        if self._fails_like_503():
            raise FakeProviderHTTPError(self.model_id)
        if self.mode == "parse":
            message = self._parse_reply(messages)
        elif self.mode == "agent":
            message = self._agent_reply(messages)
        else:
            message = AIMessage(content="Scripted fake model: bind tools or request structured output.")
        out_tokens = len(str(message.content)) // 4 + sum(len(json.dumps(tc["args"])) for tc in message.tool_calls) // 4
        in_tokens = _estimate_tokens(messages)
        message.usage_metadata = {"input_tokens": in_tokens, "output_tokens": out_tokens,
                                  "total_tokens": in_tokens + out_tokens}
        meta = dict(message.response_metadata or {})
        meta.setdefault("model_name", self.model_id)
        meta.setdefault("stop_reason", "tool_use" if message.tool_calls else "end_turn")
        message.response_metadata = meta
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _parse_reply(self, messages: list[BaseMessage]) -> AIMessage:
        from app.domain.parser_rules import parse_rules
        from app.graph.prompts import PARSE_KIND, LLMParse, hhmm, read_payload, request_from_parse_payload

        payload = read_payload(messages, kind=PARSE_KIND)
        if payload is None:
            return AIMessage(content="{}")
        request, now = request_from_parse_payload(payload)
        outcome = parse_rules(request, now)
        prefs = outcome.preferences
        start_found = not any(a.startswith("Starting at ") for a in prefs.assumptions)
        constraints = [{"kind": c.kind, "value": c.value} for c in prefs.hard_constraints]
        if self.fault == "parser_omits_diet_and_agent_selects_nonveg":
            constraints = [c for c in constraints if c["kind"] != "diet"]
        parsed = LLMParse(
            city=prefs.city, neighbourhood=prefs.neighbourhood, budget=prefs.budget,
            currency=prefs.currency if outcome.budget_currency_explicit else None,
            start_time=hhmm(prefs.start_min) if start_found and prefs.start_min < 1440 else None,
            available_minutes=None if "available_time" in outcome.missing else prefs.available_minutes,
            mood=prefs.mood or None, energy=prefs.energy.value, interests=prefs.interests,
            hard_constraints=constraints,
            soft_preferences=prefs.soft_preferences,
            group_size=prefs.group_size if prefs.group_size != 1 else None,
        )
        return AIMessage(content=parsed.model_dump_json())

    def _agent_reply(self, messages: list[BaseMessage]) -> AIMessage:
        from app.domain.schemas import Candidate
        from app.graph.prompts import context_from_payload, read_payload

        if self.fault == "llm_refusal":
            return AIMessage(content="", response_metadata={"stop_reason": "refusal"})
        payload = read_payload(messages)
        if payload is None:
            return AIMessage(content="I need the planning request JSON to start.")
        prefs, ctx, enforced = context_from_payload(payload)
        ledger: dict[str, Candidate] = {}
        for m in messages:
            if isinstance(m, ToolMessage) and isinstance(m.artifact, dict):
                for raw in m.artifact.get("candidates", []):
                    c = Candidate.model_validate(raw)
                    ledger.setdefault(c.id, c)
        ai_turns = [m for m in messages if isinstance(m, AIMessage)]
        turn = len(ai_turns) + 1
        last_tools = {tc["name"] for tc in ai_turns[-1].tool_calls} if ai_turns else set()
        last = messages[-1]

        if not ai_turns:
            return self._search_turn(prefs, ctx, payload, turn)
        if isinstance(last, HumanMessage):  # a nudge after a text-only turn
            return self._submit_turn(prefs, ctx, enforced, ledger, turn, repair=False)
        if "submit_plan" in last_tools:
            return self._submit_turn(prefs, ctx, enforced, ledger, turn, repair=True)
        if last_tools & {"search_places", "search_events"}:
            return self._check_turn(prefs, ctx, enforced, ledger, turn)
        return self._submit_turn(prefs, ctx, enforced, ledger, turn, repair=False)

    def _compose(self, prefs: Any, ctx: Any, enforced: Any, ledger: dict[str, Any]) -> Any:
        from app.domain.fallback import plan_deterministic
        from app.domain.travel import HeuristicTravel

        return plan_deterministic(list(ledger.values()), ctx, prefs, enforced, HeuristicTravel())

    def _search_turn(self, prefs: Any, ctx: Any, payload: dict[str, Any], turn: int) -> AIMessage:
        from app.domain.parser_rules import default_queries
        from app.graph.prompts import hhmm

        if self.fault == "parser_omits_diet_and_agent_selects_nonveg":  # searches as if no diet was given
            prefs = prefs.model_copy(update={"hard_constraints": [c for c in prefs.hard_constraints
                                                                  if c.kind != "diet"]})
        calls = []
        for i, q in enumerate(default_queries(prefs, ctx)[:6]):
            args: dict[str, Any] = {"intent": q.intent, "category": q.category,
                                    "reason": f"Look for {q.intent} that fits the mood and constraints"}
            if q.max_price_level is not None:
                args["max_price_level"] = q.max_price_level
            if q.open_at_min is not None:
                args["open_at"] = hhmm(q.open_at_min)
            calls.append(_tool_call("search_places", args, f"call_{turn}_{i}"))
        if payload.get("events_available"):
            calls.append(_tool_call("search_events", {
                "interests": prefs.interests, "max_price": prefs.budget,
                "reason": "Check for events on the day that match the interests"}, f"call_{turn}_ev"))
        return AIMessage(content="", tool_calls=calls)

    def _check_turn(self, prefs: Any, ctx: Any, enforced: Any, ledger: dict[str, Any], turn: int) -> AIMessage:
        draft = self._compose(prefs, ctx, enforced, ledger)
        if draft is None:
            return AIMessage(content="I couldn't find a combination that meets every hard constraint and the budget.")
        ids = [s.candidate_id for s in draft.stops]
        calls = []
        if len(ids) >= 2:
            calls.append(_tool_call("estimate_travel", {"candidate_ids": ids,
                                                        "reason": "Check the legs between the chosen stops"},
                                    f"call_{turn}_travel"))
        calls.append(_tool_call("check_plan", {**draft.model_dump(mode="json"),
                                               "reason": "Dry-run the draft against budget, time and hours"},
                                f"call_{turn}_check"))
        return AIMessage(content="", tool_calls=calls)

    def _submit_turn(self, prefs: Any, ctx: Any, enforced: Any, ledger: dict[str, Any], turn: int, *,
                     repair: bool) -> AIMessage:
        from app.domain.evidence import is_food_stop
        from app.domain.schemas import Evidence

        draft = self._compose(prefs, ctx, enforced, ledger)
        if self.fault == "text_only_final_turn":
            return AIMessage(content="Here is my plan: start at the cafe, then the park, then music. Enjoy!")
        if draft is None:
            return AIMessage(content="I couldn't find a combination that meets every hard constraint and the budget.")
        args = draft.model_dump(mode="json")
        if self.fault == "submit_plan_unknown_ids_twice":
            for i, stop in enumerate(args["stops"]):
                stop["candidate_id"] = f"fx:invented-place-{turn}-{i}"
        elif self.fault == "parser_omits_diet_and_agent_selects_nonveg" and not repair:
            nonveg = [c for c in ledger.values() if is_food_stop(c) and c.diet.get("vegetarian") == Evidence.NO]
            food_idx = [i for i, s in enumerate(draft.stops) if is_food_stop(ledger[s.candidate_id])]
            if nonveg and food_idx:
                i = food_idx[0]
                pick = nonveg[0]
                typical = pick.duration_range[1]
                args["stops"][i]["candidate_id"] = pick.id
                args["stops"][i]["duration_min"] = typical
                args["stops"][i]["plan_b_candidate_id"] = None
        args["reason"] = "Submit the checked plan" if not repair else "Resubmit after fixing the violations"
        return AIMessage(content="", tool_calls=[_tool_call("submit_plan", args, f"call_{turn}_submit")])

