"""Graph mechanics: trace integrity, deadlines, append-only history, protocol errors and caps, baseline path,
checkpoint cleanup, resume edge cases and the crash path."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import Field

import app.graph.build as build_module
import app.graph.runner as runner_module
import app.llm.registry as registry_module
from app.baseline import run_baseline
from app.graph.nodes.common import AGENT_NO_TIME, PARSER_DEGRADED
from app.graph.runner import EXPIRED, compiled_graph
from app.llm.fake import ScriptedAgentModel
from tests.graph.helpers import (
    agent_tools,
    assert_plan_sound,
    assert_trace_integrity,
    collect,
    events_json,
    finalize_data,
    make_deps,
    nodes_run,
    request_for,
    run_case,
)

TRACE_CASES = [
    "assignment_example", "vague_free_text", "impossible_budget", "model_refusal", "places_provider_outage",
    "invalid_submission_then_repair_exhausted", "agent_ends_without_submit", "places_outage_no_snapshot_city",
    "unknown_city", "weather_unavailable",
]


@pytest.mark.parametrize("case_id", TRACE_CASES)
async def test_trace_integrity(case_id: str) -> None:
    result, events, _ = await run_case(case_id)
    assert_trace_integrity(events)
    assert all(e.run_id == result.run_id for e in events)
    # agent notes are the only model text in the trace
    for e in events:
        if e.type == "tool_called" and e.node == "tools" and e.reason:
            assert len(e.reason) <= 140


async def test_run_id_and_thread_id_are_honoured() -> None:
    deps = make_deps()
    result, events = await collect(request_for("vague_free_text"), deps, run_id="r_custom123", thread_id="t_custom_thread")
    assert result.run_id == "r_custom123" and {e.run_id for e in events} == {"r_custom123"}
    assert result.thread_id == "t_custom_thread"


async def test_soft_deadline_skips_the_agent() -> None:
    deps = make_deps(run_deadline_s=5)
    t0 = time.perf_counter()
    result, events = await collect(request_for("assignment_example"), deps)
    assert time.perf_counter() - t0 < 5
    assert result.status == "fallback" and result.mode == "deterministic"
    assert AGENT_NO_TIME in result.degradations and PARSER_DEGRADED not in result.degradations  # no LLM parse
    assert "plan_agent" not in nodes_run(events)
    assert_trace_integrity(events)
    assert_plan_sound(result)


async def test_hard_deadline_returns_rejected_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def stuck(state: Any, runtime: Any) -> dict[str, Any]:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(build_module, "build_context", stuck)
    monkeypatch.setattr(runner_module, "HARD_GRACE_S", 0.3)
    deps = make_deps(run_deadline_s=1)
    t0 = time.perf_counter()
    result, events = await collect(request_for("assignment_example"), deps)
    assert time.perf_counter() - t0 < 3
    assert result.status == "rejected" and result.message and "too long" in result.message
    assert [e.type for e in events[-4:]] == ["error", "node_started", "node_completed", "result"]
    assert_trace_integrity(events)


async def test_unexpected_bug_yields_error_then_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(state: Any, runtime: Any) -> dict[str, Any]:
        raise RuntimeError("internal detail /etc/passwd line 42")

    monkeypatch.setattr(build_module, "build_context", broken)
    deps = make_deps()
    result, events = await collect(request_for("assignment_example"), deps)
    assert result.status == "rejected"
    assert result.message and result.run_id in result.message
    assert [e.type for e in events].count("error") == 1
    dumped = events_json(events)
    assert "/etc/passwd" not in dumped and "Traceback" not in dumped
    assert_trace_integrity(events)
    # the crashed thread was cleaned up
    graph = compiled_graph(deps.checkpointer)
    thread_id = events[0].data["thread_id"] if events[0].data else None
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    assert not snapshot.values


async def test_checkpoints_deleted_after_terminal_kept_while_paused() -> None:
    deps = make_deps()
    graph = compiled_graph(deps.checkpointer)
    paused, _ = await collect(request_for("vague_free_text"), deps)
    snap = await graph.aget_state({"configurable": {"thread_id": paused.thread_id}})
    assert "ask_clarification" in snap.next
    done, _ = await collect(request_for("assignment_example"), deps, thread_id="t_terminal_1")
    assert done.status == "complete"
    snap = await graph.aget_state({"configurable": {"thread_id": "t_terminal_1"}})
    assert not snap.values


async def test_resume_unknown_thread_is_rejected() -> None:
    deps = make_deps()
    result, events = await collect(None, deps, thread_id="t_does_not_exist", resume_answers={"city": "Pune"})
    assert result.status == "rejected" and result.message == EXPIRED
    assert_trace_integrity(events)


async def test_new_run_without_request_is_rejected() -> None:
    result, events = await collect(None, make_deps())
    assert result.status == "rejected"
    assert_trace_integrity(events)


async def test_append_only_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    async def keep(checkpointer: Any, thread_id: str | None) -> None:
        return None

    monkeypatch.setattr(runner_module, "delete_thread", keep)
    deps = make_deps(fault="parser_omits_diet_and_agent_selects_nonveg")  # includes a rejected submission
    result, _ = await collect(request_for("diet_dropped_by_parser"), deps, thread_id="t_history")
    assert result.status == "complete"
    graph = compiled_graph(deps.checkpointer)
    history = [s async for s in graph.aget_state_history({"configurable": {"thread_id": "t_history"}})]
    snapshots: list[list[BaseMessage]] = [list(s.values.get("messages") or []) for s in reversed(history)]
    previous: list[BaseMessage] = []
    for messages in snapshots:
        assert len(messages) >= len(previous)
        for old, new in zip(previous, messages, strict=False):
            assert old.id == new.id and old.content == new.content and type(old) is type(new)
            if isinstance(old, AIMessage):
                assert old.tool_calls == new.tool_calls  # type: ignore[attr-defined]
        previous = messages
    assert any(isinstance(m, ToolMessage) and m.status == "error" and m.name == "submit_plan" for m in previous)


async def test_baseline_runs_through_the_graph() -> None:
    deps = make_deps()
    result, events = await collect(request_for("assignment_example"), deps, model_id="baseline:deterministic")
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "deterministic"
    assert result.model == "baseline:deterministic"
    assert "plan_agent" not in nodes_run(events)
    assert finalize_data(events)["usage"]["llm_calls"] == 0
    assert not result.degradations
    reference = await run_baseline(request_for("assignment_example"), make_deps())
    assert result.plan is not None and reference.plan is not None
    assert [s.candidate.id for s in result.plan.stops] == [s.candidate.id for s in reference.plan.stops]
    assert_plan_sound(result)


async def test_baseline_clarification_resumes() -> None:
    deps = make_deps()
    paused, _ = await collect(request_for("vague_free_text"), deps, model_id="baseline:deterministic")
    assert paused.status == "needs_clarification" and paused.thread_id
    answers = {"city": "Bangalore", "budget": "around 1000", "available_time": "afternoon, 3 hours"}
    result, events = await collect(None, deps, thread_id=paused.thread_id, resume_answers=answers)
    assert result.status == "complete" and result.model == "baseline:deterministic"
    assert_trace_integrity(events)


class _Misbehaving(ScriptedAgentModel):
    """Plays scripted first turns, then the normal scripted policy."""

    script: list[AIMessage] = Field(default_factory=list)

    def _agent_reply(self, messages: list[BaseMessage]) -> AIMessage:
        turn = sum(isinstance(m, AIMessage) for m in messages)
        if turn < len(self.script):
            return self.script[turn].model_copy(deep=True)
        return super()._agent_reply(messages)


def _use_model(monkeypatch: pytest.MonkeyPatch, script: list[AIMessage]) -> None:
    original = registry_module.get_chat_model

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        if purpose == "agent":
            return _Misbehaving(model_id=model_id, fault=settings.active_fault(), role=role, script=script)
        return original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


async def test_protocol_errors_are_counted_and_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    turn = AIMessage(content="", tool_calls=[
        _call("book_table", {"reason": "book it"}, "c1"),
        # a batch with two bad queries: the good ones still run, one schema_invalid for the call
        _call("search_places", {"queries": [
            {"intent": "cafe", "category": "spaceport"},
            {"intent": "quiet vegetarian restaurant", "category": "restaurant"},
            {"category": "cafe"},
            {"intent": "live music venue", "category": "live_music_venue"}], "reason": "a"}, "c2"),
        _call("search_places", {"category": "cafe", "reason": "missing intent"}, "c3"),  # nothing to run
        _call("estimate_travel", {"candidate_ids": ["fx:nope", "fx:nada"], "reason": "x"}, "c4"),
        # the old single-query shape is still accepted
        _call("search_places", {"intent": "park for a walk", "category": "park", "reason": "d" * 300}, "c5"),
    ], invalid_tool_calls=[{"name": "search_places", "args": "{not json", "id": "c9", "error": "bad json",
                            "type": "invalid_tool_call"}])
    _use_model(monkeypatch, [turn])
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert_trace_integrity(events)
    data = finalize_data(events)
    assert data["protocol_errors"] == 5
    assert data["protocol_error_kinds"] == {"unknown_tool": 1, "schema_invalid": 2, "unknown_id": 1,
                                            "malformed_args": 1}
    codes = [(e.tool, (e.data or {}).get("code")) for e in events if e.type == "tool_failed"]
    assert ("book_table", "UNKNOWN_TOOL") in codes and ("search_places", "MALFORMED_ARGS") in codes
    assert ("search_places", "SCHEMA_INVALID") in codes and ("estimate_travel", "UNKNOWN_ID") in codes
    batch = next(e for e in events if e.type == "tool_completed" and e.tool == "search_places")
    assert batch.data and batch.data["queries"] == 2
    assert all(len(e.reason or "") <= 140 for e in events)
    assert result.status == "complete" and result.mode == "agent"
    assert_plan_sound(result)


async def test_tool_caps_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    intents = ["vegetarian restaurant", "cafe", "park", "live music venue"]
    turn = AIMessage(content="", tool_calls=[
        _call("search_places", {"intent": i, "reason": f"search {i}"}, f"s{n}") for n, i in enumerate(intents)])
    _use_model(monkeypatch, [turn])
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert_trace_integrity(events)
    capped = [e for e in events if e.type == "tool_failed" and (e.data or {}).get("code") == "CAP_REACHED"]
    assert len(capped) == 1  # three search_places calls per run
    assert agent_tools(events)["search_places"] == 4
    data = finalize_data(events)
    assert data["protocol_errors"] == 1 and data["protocol_error_kinds"] == {"cap_reached": 1}
    assert result.status == "complete"


async def test_query_budget_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    def q(intent: str, category: str) -> dict[str, str]:
        return {"intent": intent, "category": category}

    five = [q("vegetarian restaurant", "restaurant"), q("quiet cafe", "cafe"), q("park", "park"),
            q("live music", "live_music_venue"), q("bookstore", "bookstore")]
    four = [q("art gallery", "art_gallery"), q("dessert", "dessert"), q("museum", "museum"), q("garden", "garden")]
    turn = AIMessage(content="", tool_calls=[
        _call("search_places", {"queries": five, "reason": "five at once"}, "b1"),  # 4 run, 1 skipped
        _call("search_places", {"queries": four, "reason": "four more"}, "b2"),  # 4 run: budget of 8 used
        _call("search_places", {"queries": [q("lake", "lake")], "reason": "one more"}, "b3"),  # nothing left
    ])
    _use_model(monkeypatch, [turn])
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert_trace_integrity(events)
    ran = [(e.data or {}).get("queries") for e in events if e.type == "tool_completed" and e.tool == "search_places"]
    assert ran == [4, 4]
    capped = [e for e in events if e.type == "tool_failed" and (e.data or {}).get("code") == "CAP_REACHED"]
    assert len(capped) == 1
    assert finalize_data(events)["protocol_error_kinds"] == {"schema_invalid": 1, "cap_reached": 1}
    assert result.status == "complete"


async def test_turn_cap_routes_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    search = AIMessage(content="", tool_calls=[_call("search_places", {"intent": "cafe", "reason": "again"}, "t")])
    _use_model(monkeypatch, [search] * 20)
    result, events = await collect(request_for("assignment_example"), make_deps(max_agent_turns=3))
    assert result.status == "fallback"
    assert finalize_data(events)["agent_turns"] == 3
    assert any("ran out of turns" in d for d in result.degradations)
    assert_trace_integrity(events)
    assert_plan_sound(result)


async def test_interrupt_and_resume_with_sqlite_checkpointer(tmp_path: Any, caplog: pytest.LogCaptureFixture) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        await saver.setup()
        deps = make_deps()
        deps.checkpointer = saver
        with caplog.at_level("WARNING"):
            paused, _ = await collect(request_for("vague_free_text"), deps)
            answers = {"city": "Bangalore", "budget": "around 1000", "available_time": "afternoon, 3 hours"}
            result, events = await collect(None, deps, thread_id=paused.thread_id, resume_answers=answers)
        assert paused.status == "needs_clarification" and result.status == "complete"
        assert_trace_integrity(events)
        serde_warnings = [r for r in caplog.records if r.name.startswith("langgraph")]
        assert not serde_warnings, [r.getMessage() for r in serde_warnings]
        snapshot = await compiled_graph(saver).aget_state({"configurable": {"thread_id": paused.thread_id}})
        assert not snapshot.values  # deleted after the terminal result


async def test_refusal_during_places_outage_reports_places_unavailable() -> None:
    from app.domain.schemas import PlanRequest

    deps = make_deps(fault="places_http_503")  # no curated snapshot covers Jaipur
    request = PlanRequest.model_validate({**request_for("assignment_example").model_dump(), "city": "Jaipur"})
    result, events = await collect(request, deps, model_id="baseline:deterministic")
    assert result.status == "no_feasible_plan" and result.message and "temporarily unavailable" in result.message
    assert_trace_integrity(events)


async def test_unexpected_tool_exception_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.graph.nodes.execute as execute_module

    async def explode(args: Any, env: Any) -> Any:
        raise ConnectionError("raw socket error with internal detail")

    monkeypatch.setattr(execute_module, "run_search_events", explode)
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert result.status == "complete"
    failed = [e for e in events if e.type == "tool_failed" and e.tool == "search_events"]
    assert failed and (failed[0].data or {}).get("code") == "TOOL_ERROR"
    assert "internal detail" not in events_json(events)
    assert_trace_integrity(events)


class _HangsAfterCheck(ScriptedAgentModel):
    """Searches, passes check_plan, then never answers again (a slow model hitting the deadline)."""

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: Any = None, **kwargs: Any) -> Any:
        if self.mode == "agent" and sum(isinstance(m, AIMessage) for m in messages) >= 2:
            await asyncio.sleep(3600)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


async def test_deadline_after_a_clean_check_plan_uses_the_checked_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.graph.nodes.common import AGENT_TIMEOUT, AGENT_USED_CHECKED_DRAFT

    original = registry_module.get_chat_model

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        if purpose == "agent":
            return _HangsAfterCheck(model_id=model_id, role=role, check_first=True)
        return original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)
    result, events = await collect(request_for("assignment_example"), make_deps(run_deadline_s=16))
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "agent"
    assert result.degradations == [AGENT_USED_CHECKED_DRAFT] and AGENT_TIMEOUT not in result.degradations
    salvage = [e for e in events if e.type == "validation" and (e.data or {}).get("source") == "agent_checked_draft"]
    assert salvage and salvage[0].data and salvage[0].data["ok"] is True
    assert finalize_data(events)["submit_attempts"] == 0
    assert_plan_sound(result)


async def test_agent_timeout_degradation_wording_comes_from_the_fallback() -> None:
    from app.graph.nodes.common import AGENT_TIMEOUT

    result, events, _ = await run_case("model_timeout", run_deadline_s=16)
    assert AGENT_TIMEOUT == "The AI planner ran out of time; switched to the rule-based planner"
    degraded = [e for e in events if e.type == "degraded"]
    assert [(e.node, e.summary) for e in degraded] == [("fallback_planner", AGENT_TIMEOUT)]
    assert result.degradations == [AGENT_TIMEOUT]


async def test_resume_reports_only_its_own_usage() -> None:
    deps = make_deps()
    paused, paused_events = await collect(request_for("vague_free_text"), deps)
    assert finalize_data(paused_events)["usage"]["llm_calls"] == 1  # the parse
    answers = {"city": "Bangalore", "budget": "around 1000", "available_time": "afternoon, 3 hours"}
    _, events = await collect(None, deps, thread_id=paused.thread_id, resume_answers=answers)
    data = finalize_data(events)
    assert data["usage"]["llm_calls"] == data["agent_turns"] + 1  # parse again + one call per turn


async def test_concurrent_resumes_run_exactly_once() -> None:
    from app.graph.runner import ALREADY_RESUMING

    deps = make_deps()
    paused, _ = await collect(request_for("vague_free_text"), deps)
    answers = {"city": "Bangalore", "budget": "around 1000", "available_time": "afternoon, 3 hours"}
    outcomes = await asyncio.gather(*(collect(None, deps, thread_id=paused.thread_id, resume_answers=answers)
                                      for _ in range(3)))
    statuses = sorted(r.status for r, _ in outcomes)
    assert statuses == ["complete", "rejected", "rejected"]
    assert {r.message for r, _ in outcomes if r.status == "rejected"} <= {ALREADY_RESUMING, EXPIRED}
    for _, events in outcomes:
        assert_trace_integrity(events)
    later, _ = await collect(None, deps, thread_id=paused.thread_id, resume_answers=answers)
    assert later.status == "rejected" and later.message == EXPIRED


async def test_omissions_carry_display_names() -> None:
    for model in ("fake:agent", "baseline:deterministic"):
        result, _ = await collect(request_for("assignment_example"), make_deps(), model_id=model)
        assert result.plan is not None and result.plan.omitted
        ids = [o for o in result.plan.omitted if ":" in o.ref]
        assert ids and all(o.name for o in ids), result.plan.omitted


async def test_out_of_range_text_time_is_capped_with_an_assumption() -> None:
    from app.domain.schemas import PlanRequest

    request = PlanRequest.model_validate({**request_for("assignment_example").model_dump(), "available_time": "20 hours"})
    result, events = await collect(request, make_deps())
    assert result.preferences is not None and result.preferences.available_minutes == 720
    assert "Capped at 12 hours" in result.assumptions
    assert_trace_integrity(events)


async def test_text_only_ending_without_a_clean_check_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.graph.nodes.common import AGENT_NO_SUBMIT

    search = AIMessage(content="", tool_calls=[_call("search_places", {"intent": "quiet cafe", "reason": "a"}, "s1")])
    _use_model(monkeypatch, [search, AIMessage(content="Here you go!"), AIMessage(content="Done, enjoy!")])
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert result.status == "fallback" and result.mode == "deterministic"
    assert AGENT_NO_SUBMIT in result.degradations and finalize_data(events)["nudges"] == 1
    assert "enjoy" not in events_json(events).lower()
    assert_trace_integrity(events)


async def test_repair_message_carries_a_one_line_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    async def keep(checkpointer: Any, thread_id: str | None) -> None:
        return None

    monkeypatch.setattr(runner_module, "delete_thread", keep)
    deps = make_deps(fault="submit_plan_unknown_ids_twice")
    await collect(request_for("assignment_example"), deps, thread_id="t_fix")
    snapshot = await compiled_graph(deps.checkpointer).aget_state({"configurable": {"thread_id": "t_fix"}})
    replies = [m for m in snapshot.values["messages"] if isinstance(m, ToolMessage) and m.name == "submit_plan"]
    body = _json.loads(str(replies[0].content))
    assert body["fix"].startswith("Fix: stop 1: use only IDs returned by search_places or search_events")


async def test_parse_node_adds_no_constraints_outside_merge_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.graph.nodes.parse as parse_module
    from app.domain.parser_rules import merge_parse as real_merge

    seen: list[Any] = []

    def spy(llm: Any, rules: Any, request: Any) -> Any:
        out = real_merge(llm, rules, request)
        seen.append(out)
        return out

    monkeypatch.setattr(parse_module, "merge_parse", spy)
    result, _ = await collect(request_for("assignment_example"), make_deps())
    assert seen and result.preferences is not None
    keys = {c.key for c in result.preferences.hard_constraints}
    assert keys == {c.key for c in seen[-1].preferences.hard_constraints}


async def test_structured_requests_skip_the_llm_parse() -> None:
    from app.domain.schemas import PlanRequest

    result, events = await collect(request_for("assignment_example"), make_deps())
    parse = next(e for e in events if e.type == "node_completed" and e.node == "parse_preferences")
    assert parse.data and parse.data["rules_reason"] == "structured_request" and parse.data["source"] == "rules"
    assert not result.degradations and finalize_data(events)["usage"]["llm_calls"] == 2
    free = PlanRequest(free_text="Bangalore, 4 hours from 3pm, ₹2000, tired, vegetarian, love food and music")
    _, free_events = await collect(free, make_deps())
    parse = next(e for e in free_events if e.type == "node_completed" and e.node == "parse_preferences")
    assert parse.data and parse.data["rules_reason"] is None and parse.data["source"] == "llm"
    assert finalize_data(free_events)["usage"]["llm_calls"] == 3  # parse + search + submit


class _RateLimitedSecondCall(ScriptedAgentModel):
    """The primary answers its first agent call, then hits a 429 (Cerebras trial: 5 requests per minute)."""

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                         run_manager: Any = None, **kwargs: Any) -> Any:
        if self.mode == "agent" and self.role == "primary" and any(isinstance(m, AIMessage) for m in messages):
            raise RuntimeError("Error code: 429 - rate limit exceeded")
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


async def test_rate_limit_on_the_primary_mid_run_falls_through_and_sticks(monkeypatch: pytest.MonkeyPatch) -> None:
    original = registry_module.get_chat_model
    calls: list[str] = []

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        if purpose == "agent":
            calls.append(role)
            return _RateLimitedSecondCall(model_id=model_id, role=role)
        return original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)
    result, events = await collect(request_for("assignment_example"),
                                   make_deps(primary_model="fake:agent", fallback_model="fake:secondary"))
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "agent" and result.model == "fake:secondary"
    switch = [e for e in events if e.type == "degraded" and "switched to fake:secondary" in (e.summary or "")]
    assert len(switch) == 1
    assert finalize_data(events)["agent_turns"] == 2


async def test_rate_limit_wait_is_reported_in_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.ratelimit import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(1, 0.25, name="fake:agent")
    original = registry_module.get_chat_model

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        model = original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]
        return model.model_copy(update={"rate_limiter": limiter}) if purpose == "agent" else model

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)
    result, events = await collect(request_for("assignment_example"), make_deps())
    data = finalize_data(events)
    assert result.status == "complete" and data["agent_turns"] == 2
    assert 150 <= data["rate_limit_wait_ms"] <= 600  # the second turn waited for the window
    assert "rate_limit_wait_ms" not in data["usage"]
    assert_trace_integrity(events)


async def test_per_model_turn_cap_reaches_the_model_and_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.graph.nodes.agent as agent_module

    monkeypatch.setattr(agent_module, "agent_turn_cap", lambda settings, model_id: 2)
    search = AIMessage(content="", tool_calls=[_call("search_places", {"queries": ["quiet cafe"], "reason": "a"}, "s")])
    _use_model(monkeypatch, [search, search, search])
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert result.status == "fallback" and finalize_data(events)["agent_turns"] == 2
    assert any("ran out of turns" in d for d in result.degradations)
    assert_trace_integrity(events)


# --- window fill (app.domain.fill contract: fill_window / free_time_note) --------------------------------


class _Underfills(ScriptedAgentModel):
    """Submits the usual plan but with every visit at its minimum length (the live defect: 131 of 240 min)."""

    def _compose(self, prefs: Any, ctx: Any, enforced: Any, ledger: dict[str, Any]) -> Any:
        from app.domain.validator import duration_bounds

        draft = super()._compose(prefs, ctx, enforced, ledger)
        if draft is None:
            return None
        stops = [s.model_copy(update={"duration_min": duration_bounds(ledger[s.candidate_id])[0]})
                 for s in draft.stops]
        return draft.model_copy(update={"stops": stops})


def _stub_fill(monkeypatch: pytest.MonkeyPatch, fill: Any, note: Any = None) -> None:
    import app.graph.nodes.common as common

    monkeypatch.setattr(common, "fill_window", fill)
    monkeypatch.setattr(common, "free_time_note", note or (lambda plan, ctx: None))


def _use_underfilling_model(monkeypatch: pytest.MonkeyPatch) -> None:
    original = registry_module.get_chat_model

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        if purpose == "agent":
            return _Underfills(model_id=model_id, role=role)
        return original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)


def _to_typical(draft: Any, ledger: Any, ctx: Any, prefs: Any, constraints: Any, travel: Any,
                target_ratio: float = 0.85) -> Any:
    """A small, valid stretch: ten more minutes at the first (seated, non-walking) stop."""
    first = draft.stops[0]
    stops = [first.model_copy(update={"duration_min": first.duration_min + 10}), *draft.stops[1:]]
    return draft.model_copy(update={"stops": stops})


async def test_accepted_agent_draft_is_filled_and_revalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_underfilling_model(monkeypatch)
    _stub_fill(monkeypatch, _to_typical,
               note=lambda plan, ctx: f"About {ctx.end_min - plan.end_min} min of your window is still free")
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "agent" and result.plan is not None
    added = finalize_data(events)["filled_minutes"]
    assert added == 10
    accepted = next(e for e in events if e.type == "validation" and e.node == "validate_plan")
    assert accepted.data and accepted.data["filled_minutes"] == added
    assert any(w.startswith("About ") and "still free" in w for w in result.warnings)
    assert all("still free" not in s.why for s in result.plan.stops)  # never in model prose
    assert_plan_sound(result)


async def test_a_fill_that_breaks_validation_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_long(draft: Any, *args: Any, **kwargs: Any) -> Any:
        return draft.model_copy(update={"stops": [s.model_copy(update={"duration_min": 590}) for s in draft.stops]})

    _use_underfilling_model(monkeypatch)
    _stub_fill(monkeypatch, too_long)
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert result.status == "complete" and finalize_data(events)["filled_minutes"] == 0
    assert result.plan is not None and all(s.duration_min < 590 for s in result.plan.stops)


async def test_fill_errors_keep_the_submitted_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fill bug")

    _use_underfilling_model(monkeypatch)
    _stub_fill(monkeypatch, broken, note=lambda plan, ctx: 1 / 0)
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert result.status == "complete" and finalize_data(events)["filled_minutes"] == 0


async def test_checked_draft_salvage_is_filled_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_fill(monkeypatch, _to_typical)
    original = registry_module.get_chat_model

    class _UnderfillsThenHangs(_Underfills):
        async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                             run_manager: Any = None, **kwargs: Any) -> Any:
            if self.mode == "agent" and sum(isinstance(m, AIMessage) for m in messages) >= 2:
                await asyncio.sleep(3600)
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        if purpose == "agent":
            return _UnderfillsThenHangs(model_id=model_id, role=role, check_first=True)
        return original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)
    result, events = await collect(request_for("assignment_example"), make_deps(run_deadline_s=16))
    assert result.status == "complete" and result.mode == "agent"
    salvage = next(e for e in events if e.type == "validation" and (e.data or {}).get("source") == "agent_checked_draft")
    assert salvage.data and salvage.data["filled_minutes"] > 0
    assert finalize_data(events)["filled_minutes"] == salvage.data["filled_minutes"]
    assert_plan_sound(result)


async def test_real_fill_module_lengthens_an_underfilled_agent_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_underfilling_model(monkeypatch)
    result, events = await collect(request_for("assignment_example"), make_deps())
    assert result.status == "complete" and result.mode == "agent" and result.plan is not None
    added = finalize_data(events)["filled_minutes"]
    assert added > 0  # minimum-length visits (179 of 240 min) were stretched by code, with no model call
    totals = result.plan.totals
    assert totals.elapsed_min >= 179 + added and totals.elapsed_min <= totals.available_minutes
    assert finalize_data(events)["agent_turns"] == 2
    assert_plan_sound(result)


class _KeepsSearching(ScriptedAgentModel):
    """Searches on every turn while search tools are bound (the live "ran out of turns" pattern)."""

    def _agent_reply(self, messages: list[BaseMessage]) -> Any:
        from app.graph.prompts import context_from_payload, read_payload

        if "search_places" not in self.tool_names:
            return super()._agent_reply(messages)
        payload = read_payload(messages)
        assert payload is not None
        prefs, ctx, _ = context_from_payload(payload)
        turn = sum(isinstance(m, AIMessage) for m in messages) + 1
        return self._search_turn(prefs, ctx, payload, turn)


async def test_last_turn_binds_only_submit_plan_and_the_agent_still_submits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.graph.prompts import FINAL_TURN_TEXT

    seen: list[tuple[tuple[str, ...], str]] = []
    original = registry_module.get_chat_model

    class _Recording(_KeepsSearching):
        def _agent_reply(self, messages: list[BaseMessage]) -> Any:
            seen.append((tuple(self.tool_names), str(messages[-1].content)))
            return super()._agent_reply(messages)

    def fake_get(model_id: str, purpose: str, settings: Any, *, role: str = "primary") -> Any:
        if purpose == "agent":
            return _Recording(model_id=model_id, role=role)
        return original(model_id, purpose, settings, role=role)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "get_chat_model", fake_get)
    result, events = await collect(request_for("assignment_example"), make_deps(max_agent_turns=3))
    assert_trace_integrity(events)
    assert result.status == "complete" and result.mode == "agent" and not result.degradations
    assert [len(names) > 1 for names, _ in seen] == [True, True, False]
    assert seen[-1] == (("submit_plan",), FINAL_TURN_TEXT)
    finals = [(e.data or {}).get("final_turn") for e in events if e.type == "node_completed" and e.node == "plan_agent"]
    assert finals == [False, False, True]
    assert finalize_data(events)["agent_turns"] == 3
    assert_plan_sound(result)


async def test_text_on_the_last_turn_goes_straight_to_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    search = AIMessage(content="", tool_calls=[_call("search_places", {"queries": [
        {"intent": "quiet vegetarian cafe", "category": "cafe"}], "reason": "find a cafe"}, "s")])
    _use_model(monkeypatch, [search, AIMessage(content="Nothing fits, sorry.")])
    result, events = await collect(request_for("assignment_example"), make_deps(max_agent_turns=2))
    assert finalize_data(events)["agent_turns"] == 2 and finalize_data(events)["nudges"] == 0
    assert result.status == "fallback"
    assert any("didn't submit" in d for d in result.degradations)
    assert_trace_integrity(events)
