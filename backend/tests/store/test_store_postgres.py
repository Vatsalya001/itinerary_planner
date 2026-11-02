"""Postgres store and checkpointer against a real database (DATABASE_URL=postgresql://...).

Marked live: deselected by default (pytest addopts) and skipped unless DATABASE_URL points at Postgres.
Run with ``DATABASE_URL=postgresql://... uv run pytest -m live tests/store``. Uses unique keys and cleans up.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.checkpoint import open_checkpointer
from app.store.base import RunRecord
from app.store.factory import is_postgres_url
from app.store.postgres import PostgresStore
from tests.api.plan_factories import MutableClock

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not is_postgres_url(DATABASE_URL), reason="DATABASE_URL is not a Postgres URL"),
]

START = datetime(2026, 11, 5, 12, 5, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(START)


@pytest.fixture
async def store(clock: MutableClock) -> AsyncIterator[PostgresStore]:
    s = PostgresStore(DATABASE_URL, clock=clock)
    await s.init()
    yield s
    await s.close()


def _uid() -> str:
    return "test_" + secrets.token_hex(6)


async def test_plan_ttl(store: PostgresStore, clock: MutableClock) -> None:
    sid = _uid()
    await store.save_plan(sid, {"v": 1, "x": "₹"}, ttl_days=1)
    assert await store.get_plan(sid) == {"v": 1, "x": "₹"}
    clock.now = START + timedelta(days=1)
    assert await store.get_plan(sid) is None
    await store._execute("DELETE FROM plans WHERE share_id = %s", (sid,))


async def test_rate_limit_and_daily(store: PostgresStore, clock: MutableClock) -> None:
    key = _uid()
    assert await store.hit_rate_limit(key, 600, 1) == (True, 0)
    assert await store.hit_rate_limit(key, 600, 1) == (False, 900)  # sliding: free again at 12:20
    clock.now = START + timedelta(minutes=5)
    assert (await store.hit_rate_limit(key, 600, 1))[0] is False
    clock.now = START + timedelta(minutes=15)
    assert await store.hit_rate_limit(key, 600, 1) == (True, 0)
    assert await store.incr_daily(key) == 1
    assert await store.incr_daily(key) == 2
    await store._execute("DELETE FROM rate_limits WHERE key = %s", (key,))
    await store._execute("DELETE FROM daily_counters WHERE key = %s", (key,))


async def test_hit_limits_counts_only_when_every_limit_allows(store: PostgresStore) -> None:
    import asyncio

    ten, day, daily, burst_key = _uid(), _uid(), _uid(), _uid()
    windows = [(ten, 600, 3), (day, 86400, 1)]
    try:
        assert await store.hit_limits(windows, daily_key=daily, daily_cap=5) == (True, 0, None)
        allowed, retry_after, denied_by = await store.hit_limits(windows, daily_key=daily, daily_cap=5)
        assert (allowed, denied_by) == (False, day) and retry_after > 0
        rows = await store._execute("SELECT key, count FROM rate_limits WHERE key IN (%s, %s)", (ten, day))
        assert dict(rows) == {ten: 1, day: 1}  # the refused request was rolled back in every window
        assert (await store._execute("SELECT count FROM daily_counters WHERE key = %s", (daily,)))[0][0] == 1

        burst = [(burst_key, 600, 50)]
        results = await asyncio.gather(*(store.hit_limits(burst, daily_key=daily, daily_cap=4) for _ in range(10)))
        assert sum(1 for allowed, _, _ in results if allowed) == 3  # one of the cap's 4 was used above
    finally:
        await store._execute("DELETE FROM rate_limits WHERE key IN (%s, %s, %s)", (ten, day, burst_key))
        await store._execute("DELETE FROM daily_counters WHERE key = %s", (daily,))


async def test_runs_feedback_eval_summary(store: PostgresStore) -> None:
    run = RunRecord(run_id=_uid(), created_at=START, git_sha="abc", model="m", status="complete", mode="agent",
                    latency_ms=10, tool_calls={"search_places": 1}, violation_codes=["X"], degradations=["d"])
    await store.record_run(run)
    assert await store.get_run(run.run_id) == run
    sid = _uid()
    await store.save_feedback(sid, "up", "nice")
    assert await store.list_feedback(sid) == [{"rating": "up", "reason": "nice"}]
    await store.save_eval_summary({"marker": sid})
    assert (await store.get_eval_summary() or {}).get("marker") == sid
    assert await store.ping() is True
    await store._execute("DELETE FROM runs WHERE run_id = %s", (run.run_id,))
    await store._execute("DELETE FROM feedback WHERE share_id = %s", (sid,))


async def test_postgres_checkpointer_setup() -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with open_checkpointer(DATABASE_URL) as saver:
        assert isinstance(saver, AsyncPostgresSaver)
        assert await saver.aget_tuple({"configurable": {"thread_id": _uid(), "checkpoint_ns": ""}}) is None


async def test_retention(store: PostgresStore, clock: MutableClock) -> None:
    tid = _uid()
    await store.record_paused_thread(tid)
    assert tid in await store.stale_paused_threads(-1)
    assert tid not in await store.stale_paused_threads(3600)
    await store.forget_paused_thread(tid)
    assert tid not in await store.stale_paused_threads(-1)

    sid = _uid()
    await store.save_plan(sid, {"x": 1}, ttl_days=1)
    clock.now = START + timedelta(days=2)
    counts = await store.purge_expired(run_retention_days=90, feedback_retention_days=90)
    assert counts["plans"] >= 1 and set(counts) == {"plans", "runs", "feedback", "rate_limits", "daily_counters"}
    assert await store.get_plan(sid) is None
