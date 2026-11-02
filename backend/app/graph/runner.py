"""Graph runner: the single entry point the API and the eval harness use.

``stream_plan`` compiles the graph once per process and checkpointer, runs (or resumes) a thread with
``graph.astream(..., stream_mode=["custom", "updates"])`` in a producer task, and yields the TraceEvents the
nodes emit through the LangGraph stream writer. The whole call is bounded by ``settings.run_deadline_s``
(nodes route to the deterministic planner before it; the runner hard-stops a few seconds after it).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.deps import Deps
from app.domain.schemas import ClarificationQuestion, ParseOutcome, PlanRequest, PlanResult, TraceEvent
from app.graph.build import build_graph, recursion_limit
from app.graph.nodes.finalize import run_metrics
from app.graph.state import RunContext
from app.llm.usage import empty_usage
from app.trace import Tracer, new_run_id

log = logging.getLogger("app.graph")

HARD_GRACE_S = 5.0
GENERIC_ERROR = "Something went wrong while planning"
TOO_SLOW = "Planning took too long"
EXPIRED = "This question has expired or was already answered. Please start a new plan."
NO_REQUEST = "No planning request was given."
ALREADY_RESUMING = "This plan is already being resumed"

_DEFAULT_SAVER = InMemorySaver()
_COMPILED: OrderedDict[int, tuple[Any, Any]] = OrderedDict()
_MAX_COMPILED = 8
# One resume per paused thread at a time in this process (the checkpoint check below handles later callers).
# A multi-instance deployment additionally needs a claim in the shared store.
_RESUME_LOCKS: dict[str, asyncio.Lock] = {}


class RunDeadlineError(RuntimeError):
    pass


def compiled_graph(checkpointer: Any) -> Any:
    """Compile once per process and checkpointer (the checkpointer is kept alive so its id is not reused)."""
    key = id(checkpointer)
    hit = _COMPILED.get(key)
    if hit is not None and hit[0] is checkpointer:
        _COMPILED.move_to_end(key)
        return hit[1]
    graph = build_graph().compile(checkpointer=checkpointer)
    _COMPILED[key] = (checkpointer, graph)
    while len(_COMPILED) > _MAX_COMPILED:
        _COMPILED.popitem(last=False)
    return graph


def checkpointer_for(deps: Deps) -> Any:
    return deps.checkpointer if deps.checkpointer is not None else _DEFAULT_SAVER


def initial_state(request: PlanRequest, model_id: str) -> dict[str, Any]:
    return {
        "request": request.model_dump(mode="json"), "model_id": model_id, "messages": [], "ledger": {},
        "degradations": [], "extra_assumptions": [], "usage": empty_usage(), "submit_attempts": 0,
        "agent_turns": 0, "nudges": 0, "protocol_errors": 0, "tool_calls": {}, "tool_exec": {},
        "places_mode": "live", "events_failed": False, "asked_before": False, "first_submit_valid": None,
        "model_used": None, "pending_submit": None, "questions": [], "matches": None, "lookup_failed": False,
        "injection_flagged": False,
    }


async def delete_thread(checkpointer: Any, thread_id: str | None) -> None:
    if not thread_id:
        return
    try:
        await checkpointer.adelete_thread(thread_id)
    except Exception as exc:  # cleanup must never break a run
        log.warning("checkpoint cleanup failed", extra={"error_type": type(exc).__name__})


class _Run:
    """Per-call bookkeeping shared by the run body and the error handler."""

    def __init__(self, run_id: str, deps: Deps) -> None:
        self.run_id = run_id
        self.deps = deps
        self.tracer = Tracer(run_id)
        self.started = time.perf_counter()
        self.model_id = deps.settings.primary_model
        self.thread_id: str | None = None
        self.config: dict[str, Any] | None = None
        self.graph: Any = None
        self.result_sent = False
        self.terminal = True
        self.usage_base: dict[str, Any] | None = None  # usage reported by the call that paused (resume only)

    def emit(self, type_: Any, **fields: Any) -> TraceEvent:
        self.tracer.bind(None)  # runner-side events are yielded directly
        return self.tracer.emit(type_, **fields)

    def latency_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    async def values(self) -> dict[str, Any]:
        if self.graph is None or self.config is None:
            return {}
        try:
            snapshot = await self.graph.aget_state(self.config)
            return dict(snapshot.values or {})
        except Exception:
            return {}

    def closing(self, result: PlanResult, metrics: dict[str, Any]) -> list[TraceEvent]:
        """The finalize node_completed event and the single result event, for paths that end outside the graph
        (clarification pause, rejected resume, crash)."""
        events = [self.emit("node_started", node="finalize"),
                  self.emit("node_completed", node="finalize", duration_ms=0, summary=result.status, data=metrics)]
        events.append(self.emit("result", data=result.model_dump(mode="json")))
        self.result_sent = True
        return events

    def metrics(self, values: dict[str, Any], rc: RunContext | None = None) -> dict[str, Any]:
        ctx = rc or RunContext(deps=self.deps, tracer=self.tracer, run_id=self.run_id, model_id=self.model_id,
                               started=self.started, deadline=self.started, usage_base=self.usage_base)
        return run_metrics(values, values.get("model_used") or values.get("model_id") or self.model_id, ctx)  # type: ignore[arg-type]


async def stream_plan(
    request: PlanRequest | None,
    deps: Deps,
    *,
    thread_id: str | None = None,
    resume_answers: dict[str, str] | None = None,
    model_id: str | None = None,
    run_id: str | None = None,
) -> AsyncIterator[TraceEvent]:
    """Run (or resume) the planner graph and yield TraceEvents as they happen.

    * New run: ``request`` is set, ``resume_answers`` is None. A new thread_id is created when None.
    * Resume after clarification: ``thread_id`` and ``resume_answers`` are set (answers keyed by
      ClarificationQuestion.id); continues the interrupted thread via Command(resume=...).
    * ``model_id`` overrides settings.primary_model. "baseline:deterministic" runs the no-LLM path through the
      same graph (rules parse, deterministic planner).
    * The final event is always type "result" (PlanResult JSON), exactly once, preceded by a "node_completed"
      event for node "finalize" whose data holds usage and run metrics.
    * Never raises for planner failures. Unexpected bugs yield an "error" event, then a "rejected" result with
      a generic message. Checkpoints are deleted once a run reaches a terminal status.
    """
    run = _Run(run_id or new_run_id(), deps)
    checkpointer = checkpointer_for(deps)
    try:
        async for event in _run_graph(run, request, checkpointer, thread_id, resume_answers, model_id):
            yield event
    except Exception as exc:
        log.error("planner run failed", extra={"error_type": type(exc).__name__, "run_id": run.run_id},
                  exc_info=not isinstance(exc, RunDeadlineError))
        if run.result_sent:
            return
        message = TOO_SLOW if isinstance(exc, RunDeadlineError) else GENERIC_ERROR
        yield run.emit("error", summary=message)
        values = await run.values()
        result = PlanResult(run_id=run.run_id, status="rejected", model=run.model_id,
                            message=f"{message}. Please try again (reference {run.run_id}).",
                            latency_ms=run.latency_ms())
        for event in run.closing(result, run.metrics(values)):
            yield event
        run.terminal = True
    finally:
        if run.terminal and run.thread_id:
            await delete_thread(checkpointer, run.thread_id)


async def _run_graph(run: _Run, request: PlanRequest | None, checkpointer: Any, thread_id: str | None,
                     resume_answers: dict[str, str] | None, model_id: str | None) -> AsyncIterator[TraceEvent]:
    settings = run.deps.settings
    run.graph = compiled_graph(checkpointer)
    limit = recursion_limit(settings.max_agent_turns, settings.max_submit_attempts)
    resumed = resume_answers is not None
    run.model_id = model_id or settings.primary_model
    if not resumed and request is not None:
        run.thread_id = thread_id or secrets.token_urlsafe(12)
    yield run.emit("run_started", summary=("Resuming after clarification" if resumed else "Planning with "
                                           + run.model_id),
                   data={"model": run.model_id, "thread_id": run.thread_id or thread_id, "resumed": resumed})
    lock: asyncio.Lock | None = None
    if resumed:
        if not thread_id:
            for event in run.closing(PlanResult(run_id=run.run_id, status="rejected", message=EXPIRED,
                                                latency_ms=run.latency_ms()), run.metrics({})):
                yield event
            return
        lock = _RESUME_LOCKS.setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            run.terminal = False  # another call owns this thread
            for event in run.closing(PlanResult(run_id=run.run_id, status="rejected", message=ALREADY_RESUMING,
                                                latency_ms=run.latency_ms()), run.metrics({})):
                yield event
            return
        await lock.acquire()  # uncontended: never suspends, so the check above and this claim are atomic
    try:
        async for event in _run_claimed(run, request, checkpointer, thread_id, resume_answers, model_id, limit):
            yield event
    finally:
        if lock is not None and thread_id is not None:
            lock.release()
            if _RESUME_LOCKS.get(thread_id) is lock and not lock.locked():
                del _RESUME_LOCKS[thread_id]


async def _run_claimed(run: _Run, request: PlanRequest | None, checkpointer: Any, thread_id: str | None,
                       resume_answers: dict[str, str] | None, model_id: str | None,
                       limit: int) -> AsyncIterator[TraceEvent]:
    settings = run.deps.settings
    graph_input: Any
    if resume_answers is not None and thread_id:
        run.config = {"configurable": {"thread_id": thread_id}, "recursion_limit": limit}
        snapshot = await run.graph.aget_state(run.config)
        if "ask_clarification" not in (snapshot.next or ()):
            run.terminal = False  # nothing to clean up, and never touch a thread we didn't pause
            for event in run.closing(PlanResult(run_id=run.run_id, status="rejected", message=EXPIRED,
                                                latency_ms=run.latency_ms()), run.metrics({})):
                yield event
            return
        run.thread_id = thread_id
        run.model_id = model_id or snapshot.values.get("model_id") or settings.primary_model
        run.usage_base = dict(snapshot.values.get("usage") or {})  # already reported by the paused call
        answers = {str(k): str(v) for k, v in (resume_answers or {}).items()}
        graph_input = Command(resume=answers)
    else:
        if request is None or run.thread_id is None:
            for event in run.closing(PlanResult(run_id=run.run_id, status="rejected", message=NO_REQUEST,
                                                latency_ms=run.latency_ms()), run.metrics({})):
                yield event
            return
        run.config = {"configurable": {"thread_id": run.thread_id}, "recursion_limit": limit}
        if thread_id:  # a caller-chosen thread starts fresh
            await delete_thread(checkpointer, thread_id)
        graph_input = initial_state(request, run.model_id)

    rc = RunContext(deps=run.deps, tracer=run.tracer, run_id=run.run_id, model_id=run.model_id,
                    started=run.started, deadline=run.started + settings.run_deadline_s, usage_base=run.usage_base)
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def produce() -> None:
        interrupted: Any = None
        try:
            async for mode, chunk in run.graph.astream(graph_input, run.config, context=rc,
                                                        stream_mode=["custom", "updates"]):
                if mode == "custom" and isinstance(chunk, TraceEvent):
                    queue.put_nowait(("event", chunk))
                elif mode == "updates" and isinstance(chunk, dict) and "__interrupt__" in chunk:
                    interrupted = chunk["__interrupt__"]
            queue.put_nowait(("done", interrupted))
        except Exception as exc:
            queue.put_nowait(("error", exc))

    task = asyncio.create_task(produce())
    hard_deadline = run.started + settings.run_deadline_s + HARD_GRACE_S
    try:
        while True:
            remaining = hard_deadline - time.perf_counter()
            try:
                kind, item = await asyncio.wait_for(queue.get(), timeout=max(0.01, remaining))
            except TimeoutError as exc:
                raise RunDeadlineError("run deadline exceeded") from exc
            if kind == "event":
                if item.type == "result":
                    if run.result_sent:
                        continue  # contract: exactly one result per call
                    run.result_sent = True
                yield item
            elif kind == "error":
                raise item
            else:
                if item:
                    run.terminal = False
                    for event in await _clarification_events(run, item, rc):
                        yield event
                elif not run.result_sent:
                    raise RuntimeError("graph ended without a result")
                return
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def _clarification_events(run: _Run, interrupts: Any, rc: RunContext) -> list[TraceEvent]:
    values = await run.values()
    raw_questions: list[Any] = []
    for intr in interrupts or ():
        value = getattr(intr, "value", None)
        if isinstance(value, dict):
            raw_questions.extend(value.get("questions") or [])
    questions = [ClarificationQuestion.model_validate(q) for q in raw_questions]
    prefs = ParseOutcome.model_validate(values["parse"]).preferences if values.get("parse") else None
    result = PlanResult(
        run_id=run.run_id, status="needs_clarification", model=run.model_id, questions=questions,
        thread_id=run.thread_id, preferences=prefs, degradations=list(values.get("degradations") or []),
        assumptions=list(prefs.assumptions) if prefs else [], latency_ms=run.latency_ms(),
    )
    return run.closing(result, run_metrics(values, run.model_id, rc))  # type: ignore[arg-type]


async def run_plan(request: PlanRequest | None, deps: Deps, **kwargs: Any) -> tuple[PlanResult, list[TraceEvent]]:
    """Collect stream_plan into (result, events). Used by JSON responses and the eval runner."""
    events: list[TraceEvent] = []
    result: PlanResult | None = None
    async for event in stream_plan(request, deps, **kwargs):
        events.append(event)
        if event.type == "result":
            result = PlanResult.model_validate(event.data or {})
    if result is None:  # pragma: no cover - stream_plan always ends with a result
        raise RuntimeError("planner produced no result")
    return result, events
