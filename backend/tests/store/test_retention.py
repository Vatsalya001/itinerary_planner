"""Retention sweep: expired plans, old runs/feedback, abandoned clarification checkpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver

from app.store import retention
from app.store.sqlite import SQLiteStore
from tests.api.plan_factories import MutableClock

START = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(START)


@pytest.fixture
async def store(clock: MutableClock) -> AsyncIterator[SQLiteStore]:
    s = SQLiteStore(":memory:", clock=clock)
    await s.init()
    yield s
    await s.close()


def _cfg(thread_id: str) -> Any:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def _checkpoint(saver: InMemorySaver, thread_id: str) -> None:
    await saver.aput(_cfg(thread_id), empty_checkpoint(), {}, {})  # type: ignore[arg-type]


async def test_sweep_deletes_only_stale_paused_checkpoints(store: SQLiteStore, clock: MutableClock,
                                                           caplog: pytest.LogCaptureFixture) -> None:
    saver = InMemorySaver()
    for tid in ("t_old", "t_fresh"):
        await _checkpoint(saver, tid)
    await store.record_paused_thread("t_old")
    await store.save_plan("share_x", {"result": {"preferences": {"mood": "never logged"}}}, ttl_days=1)
    clock.now = START + timedelta(hours=20)
    await store.record_paused_thread("t_fresh")
    clock.now = START + timedelta(hours=25)

    caplog.set_level(logging.INFO, logger="app.retention")
    counts = await retention.sweep(store, saver)
    assert counts["paused_threads"] == 1 and counts["plans"] == 1
    assert await saver.aget_tuple(_cfg("t_old")) is None
    assert await saver.aget_tuple(_cfg("t_fresh")) is not None
    assert await store.stale_paused_threads(-1) == ["t_fresh"]
    logged = "\n".join(r.getMessage() + json.dumps(r.__dict__, default=str) for r in caplog.records)
    assert "t_old" not in logged and "never logged" not in logged  # counts only


async def test_failed_checkpoint_delete_is_retried_next_sweep(store: SQLiteStore, clock: MutableClock) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.fail = True
            self.deleted: list[str] = []

        async def adelete_thread(self, thread_id: str) -> None:
            if self.fail:
                raise OSError("disk busy")
            self.deleted.append(thread_id)

    saver = Flaky()
    await store.record_paused_thread("t_old")
    clock.now = START + timedelta(days=2)
    counts = await retention.sweep(store, saver)
    assert counts["paused_threads"] == 0 and counts["paused_threads_failed"] == 1
    assert await store.stale_paused_threads(-1) == ["t_old"]
    saver.fail = False
    assert (await retention.sweep(store, saver))["paused_threads"] == 1
    assert saver.deleted == ["t_old"] and await store.stale_paused_threads(-1) == []


async def test_sweep_tolerates_missing_store_or_methods() -> None:
    assert await retention.sweep(None, None) == {}
    assert await retention.sweep(object(), InMemorySaver()) == {}


async def test_loop_runs_immediately_repeats_and_survives_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_sweep(store: Any, checkpointer: Any) -> dict[str, int]:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("db down")
        return {}

    monkeypatch.setattr(retention, "sweep", fake_sweep)
    task = asyncio.create_task(retention.retention_loop(None, None, interval_s=0.01))
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(calls) >= 3  # first sweep at once, an error did not stop the loop
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
