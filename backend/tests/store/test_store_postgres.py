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
    assert await store.hit_rate_limit(key, 600, 1) == (False, 300)
    clock.now = START + timedelta(minutes=5)
    assert await store.hit_rate_limit(key, 600, 1) == (True, 0)
    assert await store.incr_daily(key) == 1
    assert await store.incr_daily(key) == 2
    await store._execute("DELETE FROM rate_limits WHERE key = %s", (key,))
    await store._execute("DELETE FROM daily_counters WHERE key = %s", (key,))


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
