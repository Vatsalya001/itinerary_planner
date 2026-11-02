"""HTTP API: plan (SSE or JSON), resume, shared plans, feedback, eval summary, health.

Planner runs execute in background tasks so a client disconnect never cancels a run: the plan still reaches
``finalize`` and is persisted (IMPLEMENTATION_PLAN.md section 9). The "result" event is intercepted before it is
forwarded: complete/fallback plans get a share ID and are stored in redacted form, and every run is written to
the run log. Persistence failures are logged and never break the response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.responses import Response, StreamingResponse

from app.api.security import client_ip, rate_limit_subject
from app.api.sse import SSE_HEADERS, STREAM_END, SafeJSONResponse, StreamEnd, event_stream
from app.deps import Deps
from app.domain.schemas import PlanRequest, PlanResult, TraceEvent
from app.graph import runner as graph_runner
from app.observability import request_id_var, run_id_var
from app.store import redact
from app.store.base import RunRecord
from app.store.sqlite import seconds_until_utc_midnight
from app.trace import new_run_id

log = logging.getLogger("app.api")

router = APIRouter(prefix="/api")

ID_PATTERN = r"^[A-Za-z0-9_-]{6,64}$"
_ID_RE = re.compile(ID_PATTERN)
SHAREABLE_STATUSES = {"complete", "fallback"}
EVALS_SUMMARY_PATH = Path(__file__).resolve().parents[2] / "evals" / "results" / "summary.json"
GENERIC_ERROR = "Something went wrong"
# Share-link views re-fetch Google display fields (paid Place Details calls), so reads are limited per IP too.
READS_PER_10MIN = 60
# Clarification resumes continue a run that was already counted, so they have their own (generous) limit and never
# count toward the per-IP plan limits or the global daily plan cap.
RESUMES_PER_10MIN = 30
RESUME_EXPIRED_DETAIL = "This plan can no longer be resumed"


def new_share_id() -> str:
    return secrets.token_urlsafe(8)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answers: dict[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_]{1,40}$")],
        Annotated[str, StringConstraints(min_length=1, max_length=200)],
    ] = Field(min_length=1, max_length=5)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=300)


# ---------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------


def rejected(status_code: int, detail: Any, headers: dict[str, str] | None = None) -> SafeJSONResponse:
    return SafeJSONResponse({"status": "rejected", "detail": detail}, status_code=status_code, headers=headers)


def get_deps(request: Request) -> Deps:
    deps: Deps | None = getattr(request.app.state, "deps", None)
    if deps is None:
        raise RuntimeError("app dependencies are not initialised (lifespan did not run)")
    return deps


def wants_sse(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "").lower()


def _track(request: Request, task: asyncio.Task[Any]) -> None:
    tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _subject(request: Request, deps: Deps) -> str:
    """Rate-limit key material: the client IP (IPv6 by /64)."""
    return rate_limit_subject(client_ip(request, deps.settings.trust_proxy_headers))


async def _simple_limit(request: Request, deps: Deps, bucket: str, limit: int, noun: str) -> SafeJSONResponse | None:
    """One per-IP 10-minute sliding limit. Fails open (logged) if the store errors."""
    if deps.store is None:
        return None
    try:
        allowed, retry_after = await deps.store.hit_rate_limit(f"ip:{_subject(request, deps)}:{bucket}", 600, limit)
    except Exception as exc:
        log.error("rate limit check failed; allowing request", extra={"error_type": type(exc).__name__})
        return None
    if allowed:
        return None
    log.info("rate limited", extra={"bucket": bucket})
    return rejected(429, f"Too many {noun}; try again in {retry_after} seconds",
                    headers={"Retry-After": str(retry_after)})


async def check_read_limit(request: Request, deps: Deps) -> SafeJSONResponse | None:
    """Per-IP limit for share views and feedback."""
    return await _simple_limit(request, deps, "reads", READS_PER_10MIN, "requests")


async def check_resume_limit(request: Request, deps: Deps) -> SafeJSONResponse | None:
    """Per-IP limit for clarification resumes (separate from the plan limits and the daily cap)."""
    return await _simple_limit(request, deps, "resume", RESUMES_PER_10MIN, "answers")


async def check_limits(request: Request, deps: Deps) -> SafeJSONResponse | None:
    """New plans: per-IP sliding windows (10 min, day), then the global daily plan cap. Fails open (logged)."""
    settings = deps.settings
    store = deps.store
    if store is None:
        return None
    subject = _subject(request, deps)
    try:
        windows = ((f"ip:{subject}:10m", 600, settings.rate_limit_per_10min),
                   (f"ip:{subject}:day", 86400, settings.rate_limit_per_day))
        for key, window_s, limit in windows:
            allowed, retry_after = await store.hit_rate_limit(key, window_s, limit)
            if not allowed:
                log.info("rate limited", extra={"limit_window_s": window_s})
                return rejected(429, f"Too many plans; try again in {retry_after} seconds",
                                headers={"Retry-After": str(retry_after)})
        count = await store.incr_daily("plans")
        if count > settings.daily_plan_cap:
            retry_after = seconds_until_utc_midnight(datetime.now(UTC))
            log.warning("daily plan cap reached", extra={"daily_plan_cap": settings.daily_plan_cap})
            return rejected(429, "The planner has reached today's limit. Please try again tomorrow.",
                            headers={"Retry-After": str(retry_after)})
    except Exception as exc:
        log.error("rate limit check failed; allowing request", extra={"error_type": type(exc).__name__})
    return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_run_record(result: PlanResult, finalize: dict[str, Any] | None, deps: Deps) -> RunRecord:
    """Run-log row: IDs, counts, codes and usage only (never free text, mood or constraint text)."""
    data: dict[str, Any] = finalize if isinstance(finalize, dict) else {}
    usage_raw = data.get("usage")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    tools_raw = data.get("tool_calls")
    tool_calls_raw: dict[str, Any] = tools_raw if isinstance(tools_raw, dict) else {}
    cost = usage.get("cost_usd")
    model = data.get("model") if isinstance(data.get("model"), str) else None
    codes: list[str] = []
    for v in result.violations:
        if v.code not in codes:
            codes.append(v.code)
    return RunRecord(
        run_id=result.run_id,
        created_at=datetime.now(UTC),
        git_sha=deps.settings.git_sha,
        model=model or result.model,
        status=result.status,
        mode=result.mode,
        latency_ms=result.latency_ms,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        tool_calls={str(k): _int(v) for k, v in tool_calls_raw.items()},
        violation_codes=codes,
        submit_attempts=_int(data.get("submit_attempts")),
        degradations=[str(d) for d in result.degradations],
    )


async def persist_result(result: PlanResult, finalize: dict[str, Any] | None, deps: Deps,
                         started: float, resumed_thread: str | None = None) -> PlanResult:
    """Share link (complete/fallback) + run log + paused-thread bookkeeping (for checkpoint retention).

    Returns the result with share_id/latency set when available.
    """
    if result.latency_ms is None:
        result = result.model_copy(update={"latency_ms": int((time.perf_counter() - started) * 1000)})
    store = deps.store
    if store is not None and result.status in SHAREABLE_STATUSES:
        share_id = new_share_id()
        shared = result.model_copy(update={"share_id": share_id})
        try:
            await store.save_plan(share_id, redact.for_storage(shared), ttl_days=deps.settings.share_ttl_days)
            result = shared
        except Exception as exc:
            log.error("saving shared plan failed", extra={"error_type": type(exc).__name__})
    if store is not None:
        try:
            await store.record_run(build_run_record(result, finalize, deps))
        except Exception as exc:
            log.error("recording run failed", extra={"error_type": type(exc).__name__})
    await _track_paused_thread(store, result, resumed_thread)
    log.info("plan finished", extra={
        "status": result.status, "mode": result.mode, "model": result.model, "latency_ms": result.latency_ms,
        "shared": result.share_id is not None, "degradations": len(result.degradations),
    })
    return result


async def _track_paused_thread(store: Any, result: PlanResult, resumed_thread: str | None) -> None:
    """Remember threads paused at a clarification; forget them once a resume ends the thread."""
    if store is None or not hasattr(store, "record_paused_thread"):
        return
    try:
        if result.status == "needs_clarification" and result.thread_id:
            await store.record_paused_thread(result.thread_id)
        elif resumed_thread:
            await store.forget_paused_thread(resumed_thread)
    except Exception as exc:
        log.error("paused-thread bookkeeping failed", extra={"error_type": type(exc).__name__})


def is_expired_resume(result: PlanResult) -> bool:
    """The runner's answer to a resume of an unknown, finished or expired thread."""
    expired = getattr(graph_runner, "EXPIRED", None)
    return result.status == "rejected" and expired is not None and result.message == expired


def _crash_result(run_id: str) -> PlanResult:
    return PlanResult(run_id=run_id, status="rejected",
                      message=f"{GENERIC_ERROR}. Please try again (reference {run_id}).")


async def produce_events(queue: asyncio.Queue[TraceEvent | StreamEnd], deps: Deps, run_id: str,
                         plan_request: PlanRequest | None, thread_id: str | None,
                         answers: dict[str, str] | None) -> None:
    """Background producer: drive stream_plan, persist at the result event, push events to the queue."""
    token = run_id_var.set(run_id)
    started = time.perf_counter()
    finalize: dict[str, Any] | None = None
    got_result = False
    last_seq = 0
    try:
        stream = graph_runner.stream_plan(plan_request, deps, thread_id=thread_id, resume_answers=answers,
                                          run_id=run_id)
        async for event in stream:
            last_seq = max(last_seq, event.seq)
            if event.type == "node_completed" and event.node == "finalize":
                finalize = event.data
            if event.type == "result":
                if got_result:
                    continue  # contract: exactly one result per run
                result = PlanResult.model_validate(event.data or {})
                got_result = True
                result = await persist_result(result, finalize, deps, started, resumed_thread=thread_id)
                event = event.model_copy(update={"data": result.model_dump(mode="json")})
            queue.put_nowait(event)
    except Exception as exc:
        log.exception("planner run failed", extra={"error_type": type(exc).__name__})
        _capture(exc)
    finally:
        try:
            if not got_result:
                last_seq += 1
                queue.put_nowait(TraceEvent(type="error", run_id=run_id, seq=last_seq, ts=datetime.now(UTC),
                                            summary=GENERIC_ERROR))
                result = await persist_result(_crash_result(run_id), finalize, deps, started)
                last_seq += 1
                queue.put_nowait(TraceEvent(type="result", run_id=run_id, seq=last_seq, ts=datetime.now(UTC),
                                            data=result.model_dump(mode="json")))
        finally:
            queue.put_nowait(STREAM_END)
            run_id_var.reset(token)


async def run_json(deps: Deps, run_id: str, plan_request: PlanRequest | None, thread_id: str | None,
                   answers: dict[str, str] | None) -> PlanResult | None:
    """JSON mode: run_plan, then persist. Returns None if the planner crashed (caller answers 500)."""
    token = run_id_var.set(run_id)
    started = time.perf_counter()
    try:
        kwargs: dict[str, Any] = {"run_id": run_id}
        if thread_id is not None:
            kwargs.update(thread_id=thread_id, resume_answers=answers)
        try:
            result, events = await graph_runner.run_plan(plan_request, deps, **kwargs)
        except Exception as exc:
            log.exception("planner run failed", extra={"error_type": type(exc).__name__})
            _capture(exc)
            await persist_result(_crash_result(run_id), None, deps, started)
            return None
        finalize = next((e.data for e in reversed(events) if e.type == "node_completed" and e.node == "finalize"),
                        None)
        return await persist_result(result, finalize, deps, started, resumed_thread=thread_id)
    finally:
        run_id_var.reset(token)


def _capture(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:  # pragma: no cover
        pass


async def start_run(request: Request, deps: Deps, plan_request: PlanRequest | None, *,
                    thread_id: str | None = None, answers: dict[str, str] | None = None) -> Response:
    run_id = new_run_id()
    if wants_sse(request):
        queue: asyncio.Queue[TraceEvent | StreamEnd] = asyncio.Queue()
        task = asyncio.create_task(produce_events(queue, deps, run_id, plan_request, thread_id, answers))
        _track(request, task)
        return StreamingResponse(event_stream(queue), media_type="text/event-stream", headers=SSE_HEADERS)

    job = asyncio.create_task(run_json(deps, run_id, plan_request, thread_id, answers))
    _track(request, job)
    result = await asyncio.shield(job)  # a cancelled request does not cancel the run
    if result is None:
        return SafeJSONResponse({"status": "rejected", "detail": GENERIC_ERROR, "run_id": run_id,
                                 "request_id": request_id_var.get()}, status_code=500)
    if thread_id is not None and is_expired_resume(result):
        # SSE keeps the in-stream rejected result (status is already 200); JSON clients get a status code.
        return SafeJSONResponse({"status": "rejected", "detail": RESUME_EXPIRED_DETAIL, "message": result.message,
                                 "run_id": result.run_id}, status_code=404)
    return SafeJSONResponse(result.model_dump(mode="json"))


# ---------------------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------------------


@router.post("/plan", response_model=None)
async def create_plan(request: Request, body: PlanRequest) -> Response:
    deps = get_deps(request)
    limited = await check_limits(request, deps)
    if limited is not None:
        return limited
    return await start_run(request, deps, body)


@router.post("/plan/{thread_id}/resume", response_model=None)
async def resume_plan(request: Request, body: ResumeRequest,
                      thread_id: Annotated[str, PathParam(pattern=ID_PATTERN)]) -> Response:
    deps = get_deps(request)
    limited = await check_resume_limit(request, deps)
    if limited is not None:
        return limited
    return await start_run(request, deps, None, thread_id=thread_id, answers=dict(body.answers))


_NOT_FOUND = {"detail": "Plan not found or expired"}


async def _load_record(deps: Deps, share_id: str) -> dict[str, Any] | None:
    if not _ID_RE.match(share_id) or deps.store is None:
        return None
    record: dict[str, Any] | None = await deps.store.get_plan(share_id)
    return record


@router.get("/plans/{share_id}", response_model=None)
async def get_shared_plan(request: Request, share_id: str) -> Response:
    deps = get_deps(request)
    limited = await check_read_limit(request, deps)
    if limited is not None:
        return limited
    record = await _load_record(deps, share_id)
    if record is None:
        return SafeJSONResponse(_NOT_FOUND, status_code=404)
    result = await redact.rehydrate(record, deps.places, now=deps.now())
    return SafeJSONResponse(result.model_dump(mode="json"))


@router.post("/plans/{share_id}/feedback", response_model=None)
async def plan_feedback(request: Request, share_id: str, body: FeedbackRequest) -> Response:
    deps = get_deps(request)
    limited = await check_read_limit(request, deps)
    if limited is not None:
        return limited
    record = await _load_record(deps, share_id)
    if record is None:
        return SafeJSONResponse(_NOT_FOUND, status_code=404)
    await deps.store.save_feedback(share_id, body.rating, body.reason or None)
    return Response(status_code=204)


@router.get("/evals/summary", response_model=None)
async def evals_summary(request: Request) -> Response:
    deps = get_deps(request)
    summary: dict[str, Any] | None = None
    if deps.store is not None:
        try:
            summary = await deps.store.get_eval_summary()
        except Exception as exc:
            log.error("reading eval summary failed", extra={"error_type": type(exc).__name__})
    path = EVALS_SUMMARY_PATH
    if summary is None and path.is_file():
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("eval summary file unreadable", extra={"error_type": type(exc).__name__})
    if summary is None:
        return SafeJSONResponse({"detail": "No eval summary yet"}, status_code=404)
    return SafeJSONResponse(summary)


@router.get("/health", response_model=None)
async def health(request: Request) -> Response:
    deps = getattr(request.app.state, "deps", None)
    db = "error"
    if deps is not None and deps.store is not None:
        try:
            ping = getattr(deps.store, "ping", None)
            ok = await ping() if ping is not None else (await deps.store.get_eval_summary(), True)[1]
            db = "ok" if ok else "error"
        except Exception as exc:
            log.error("health db check failed", extra={"error_type": type(exc).__name__})
    settings = deps.settings if deps is not None else request.app.state.settings
    body = {
        "status": "ok" if db == "ok" else "degraded",
        "version": settings.git_sha,
        "env": settings.app_env,
        "primary_model": settings.primary_model,
        "places_provider": settings.resolved_places_provider(),
        "events_provider": settings.resolved_events_provider(),
        "db": db,
    }
    return SafeJSONResponse(body, headers={"Cache-Control": "no-store"})
