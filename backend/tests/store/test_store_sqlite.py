"""SQLite store (in-memory and file), store factory and checkpointer factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.checkpoint import checkpoint_path_for, open_checkpointer
from app.store.base import RunRecord
from app.store.factory import create_store
from app.store.postgres import PostgresStore, normalize_pg_url
from app.store.sqlite import (
    SQLiteStore,
    seconds_until_utc_midnight,
    sliding_estimate,
    sliding_retry_after,
    sqlite_path_from_url,
    window_bounds,
)
from tests.api.plan_factories import MutableClock

START = datetime(2026, 11, 5, 12, 5, 0, tzinfo=UTC)  # 5 minutes into a 10-minute window


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(START)


@pytest.fixture(params=["memory", "file"])
async def store(request: pytest.FixtureRequest, tmp_path: Path, clock: MutableClock) -> AsyncIterator[SQLiteStore]:
    path = ":memory:" if request.param == "memory" else str(tmp_path / "nested" / "app.db")
    s = SQLiteStore(path, clock=clock)
    await s.init()
    yield s
    await s.close()


async def test_plan_save_get_and_ttl_expiry(store: SQLiteStore, clock: MutableClock) -> None:
    record = {"v": 1, "result": {"run_id": "r_1", "status": "complete", "note": "₹ and unicode ok"}}
    await store.save_plan("share_abc", record, ttl_days=30)
    assert await store.get_plan("share_abc") == record
    assert await store.get_plan("unknown") is None

    clock.now = START + timedelta(days=29, hours=23)
    assert await store.get_plan("share_abc") == record
    clock.now = START + timedelta(days=30)
    assert await store.get_plan("share_abc") is None  # expired exactly at the TTL


async def test_plan_upsert_overwrites(store: SQLiteStore) -> None:
    await store.save_plan("share_abc", {"a": 1}, ttl_days=1)
    await store.save_plan("share_abc", {"a": 2}, ttl_days=1)
    assert await store.get_plan("share_abc") == {"a": 2}


async def test_rate_limit_sliding_window(store: SQLiteStore, clock: MutableClock) -> None:
    key = "ip:203.0.113.9:10m"
    assert await store.hit_rate_limit(key, 600, 2) == (True, 0)
    assert await store.hit_rate_limit(key, 600, 2) == (True, 0)
    assert await store.hit_rate_limit(key, 600, 2) == (False, 600)  # the two 12:05 hits fade out by 12:15
    clock.now = START + timedelta(minutes=4, seconds=30)
    assert await store.hit_rate_limit(key, 600, 2) == (False, 330)
    assert await store.hit_rate_limit("ip:198.51.100.1:10m", 600, 2) == (True, 0)  # keys are independent

    clock.now = START + timedelta(minutes=5)  # 12:10: the previous window still weighs 2 * 100%
    assert await store.hit_rate_limit(key, 600, 2) == (False, 300)
    clock.now = START + timedelta(minutes=10)  # 12:15: weight 2 * 50% = 1, so one more fits
    assert await store.hit_rate_limit(key, 600, 2) == (True, 0)
    assert (await store.hit_rate_limit(key, 600, 2))[0] is False


async def test_rate_limit_blocks_the_boundary_burst(store: SQLiteStore, clock: MutableClock) -> None:
    key = "ip:203.0.113.9:10m"
    clock.now = datetime(2026, 11, 5, 12, 9, 59, tzinfo=UTC)
    assert [(await store.hit_rate_limit(key, 600, 3))[0] for _ in range(3)] == [True, True, True]
    clock.now = datetime(2026, 11, 5, 12, 10, 1, tzinfo=UTC)  # a fixed window would allow 3 more here
    assert (await store.hit_rate_limit(key, 600, 3))[0] is False


async def test_denied_hits_are_not_counted(store: SQLiteStore, clock: MutableClock) -> None:
    key = "ip:203.0.113.9:10m"
    assert (await store.hit_rate_limit(key, 600, 1))[0] is True
    for _ in range(20):  # hammering after a 429 does not extend the lockout
        assert (await store.hit_rate_limit(key, 600, 1))[0] is False
    clock.now = START + timedelta(minutes=15)  # 12:20: two windows later
    assert (await store.hit_rate_limit(key, 600, 1))[0] is True


async def test_rate_limit_day_window(store: SQLiteStore, clock: MutableClock) -> None:
    key = "ip:203.0.113.9:day"
    assert (await store.hit_rate_limit(key, 86400, 2))[0] is True
    assert (await store.hit_rate_limit(key, 86400, 2))[0] is True
    allowed, retry = await store.hit_rate_limit(key, 86400, 2)
    assert allowed is False and retry == 11 * 3600 + 55 * 60 + 43200
    clock.now = datetime(2026, 11, 6, 12, 0, 1, tzinfo=UTC)  # yesterday's 2 hits now weigh < 1
    assert (await store.hit_rate_limit(key, 86400, 2))[0] is True


async def test_daily_counter_per_key_and_utc_day(store: SQLiteStore, clock: MutableClock) -> None:
    assert [await store.incr_daily("plans") for _ in range(3)] == [1, 2, 3]
    assert await store.incr_daily("other") == 1
    clock.now = datetime(2026, 11, 5, 23, 59, 59, tzinfo=UTC)
    assert await store.incr_daily("plans") == 4
    clock.now = datetime(2026, 11, 6, 0, 0, 0, tzinfo=UTC)
    assert await store.incr_daily("plans") == 1


async def test_eval_summary_latest_wins(store: SQLiteStore) -> None:
    assert await store.get_eval_summary() is None
    await store.save_eval_summary({"generated": 1, "rows": []})
    await store.save_eval_summary({"generated": 2, "rows": [{"model": "baseline:deterministic"}]})
    assert await store.get_eval_summary() == {"generated": 2, "rows": [{"model": "baseline:deterministic"}]}


async def test_run_record_roundtrip(store: SQLiteStore) -> None:
    run = RunRecord(
        run_id="r_abc", created_at=START, git_sha="abc1234", model="anthropic:claude-opus-5-5", status="fallback",
        mode="deterministic", latency_ms=1234, input_tokens=100, output_tokens=50, cost_usd=0.01,
        tool_calls={"search_places": 3}, violation_codes=["BUDGET_EXCEEDED"], submit_attempts=3,
        degradations=["model_timeout"],
    )
    await store.record_run(run)
    assert await store.get_run("r_abc") == run
    await store.record_run(run.model_copy(update={"status": "complete"}))
    stored = await store.get_run("r_abc")
    assert stored is not None and stored.status == "complete"
    assert await store.get_run("missing") is None


async def test_feedback(store: SQLiteStore) -> None:
    await store.save_feedback("share_abc", "up", None)
    await store.save_feedback("share_abc", "down", "too far")
    assert await store.list_feedback("share_abc") == [{"rating": "up", "reason": None},
                                                      {"rating": "down", "reason": "too far"}]


async def test_ping(store: SQLiteStore) -> None:
    assert await store.ping() is True


async def test_file_store_persists_across_reopen(tmp_path: Path, clock: MutableClock) -> None:
    path = str(tmp_path / "app.db")
    first = SQLiteStore(path, clock=clock)
    await first.init()
    await first.save_plan("share_keep", {"x": 1})
    await first.incr_daily("plans")
    await first.close()

    second = SQLiteStore(path, clock=clock)
    await second.init()
    assert await second.get_plan("share_keep") == {"x": 1}
    assert await second.incr_daily("plans") == 2
    await second.close()


async def test_uninitialised_store_raises() -> None:
    with pytest.raises(RuntimeError):
        await SQLiteStore(":memory:").get_plan("x")


def test_helpers() -> None:
    start = int(START.timestamp()) - 300
    assert window_bounds(START.timestamp(), 600) == (start, start - 600, 300.0)
    assert sliding_estimate(4, 1, 150, 600) == 4.0
    assert sliding_retry_after(0, 0, 5, 300, 600) == 300
    assert sliding_retry_after(4, 1, 3, 0, 600) == 450  # until 4 * (1 - t/600) + 2 <= 3
    assert sliding_retry_after(0, 3, 3, 300, 600) == 300 + 200  # next window, until 3 * (1 - t) + 1 <= 3
    assert seconds_until_utc_midnight(START) == 11 * 3600 + 55 * 60
    assert sqlite_path_from_url("sqlite:///:memory:") == ":memory:"
    assert sqlite_path_from_url("sqlite:///./data/app.db") == "./data/app.db"
    assert sqlite_path_from_url("sqlite:////abs/app.db") == "/abs/app.db"
    assert normalize_pg_url("postgres://u:p@h/db") == "postgresql://u:p@h/db"
    assert normalize_pg_url("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"


def test_factory() -> None:
    mem = create_store("sqlite:///:memory:")
    assert isinstance(mem, SQLiteStore) and mem.is_memory
    file_store = create_store("sqlite:///./data/app.db")
    assert isinstance(file_store, SQLiteStore) and file_store.path == "./data/app.db"
    pg = create_store("postgresql://user:pw@localhost:5432/app")
    assert isinstance(pg, PostgresStore)
    assert isinstance(create_store("postgres://user:pw@localhost/app"), PostgresStore)
    with pytest.raises(ValueError):
        create_store("mysql://nope")


def test_checkpoint_path() -> None:
    assert checkpoint_path_for("sqlite:///:memory:") is None
    assert checkpoint_path_for("postgresql://h/db") is None
    assert checkpoint_path_for("sqlite:///./data/app.db") == "data/app.checkpoints.db"
    assert checkpoint_path_for("sqlite:////tmp/a.db") == "/tmp/a.checkpoints.db"
    assert checkpoint_path_for("sqlite:////tmp/b.db") == "/tmp/b.checkpoints.db"  # never shared
    assert checkpoint_path_for("sqlite:////tmp/plain") == "/tmp/plain.checkpoints.db"


async def test_open_checkpointer_memory_and_sqlite(tmp_path: Path) -> None:
    async with open_checkpointer("sqlite:///:memory:") as saver:
        assert isinstance(saver, InMemorySaver)
    async with open_checkpointer(f"sqlite:///{tmp_path}/sub/app.db") as saver:
        assert isinstance(saver, AsyncSqliteSaver)
        assert (tmp_path / "sub" / "app.checkpoints.db").exists()
        assert await saver.aget_tuple({"configurable": {"thread_id": "nope", "checkpoint_ns": ""}}) is None


async def test_purge_expired(store: SQLiteStore, clock: MutableClock) -> None:
    await store.save_plan("share_old", {"x": 1}, ttl_days=30)
    await store.save_plan("share_new", {"x": 2}, ttl_days=60)
    old_run = RunRecord(run_id="r_old", created_at=START, git_sha="a", model=None, status="complete", mode=None,
                        latency_ms=None)
    await store.record_run(old_run)
    await store.save_feedback("share_old", "down", "free text reason")
    await store.hit_rate_limit("ip:x:10m", 600, 5)
    await store.incr_daily("plans")

    assert await store.purge_expired(run_retention_days=90, feedback_retention_days=90) == {
        "plans": 0, "runs": 0, "feedback": 0, "rate_limits": 0, "daily_counters": 0}

    clock.now = START + timedelta(days=91)
    run_new = old_run.model_copy(update={"run_id": "r_new", "created_at": clock.now - timedelta(days=1)})
    await store.record_run(run_new)
    counts = await store.purge_expired(run_retention_days=90, feedback_retention_days=90)
    assert counts == {"plans": 2, "runs": 1, "feedback": 1, "rate_limits": 1, "daily_counters": 1}
    assert await store.get_run("r_old") is None and await store.get_run("r_new") is not None
    assert await store.list_feedback("share_old") == []


async def test_paused_threads(store: SQLiteStore, clock: MutableClock) -> None:
    await store.record_paused_thread("t_a")
    clock.now = START + timedelta(hours=2)
    await store.record_paused_thread("t_b")
    assert await store.stale_paused_threads(3600) == ["t_a"]
    clock.now = START + timedelta(hours=27)
    assert await store.stale_paused_threads(24 * 3600) == ["t_a", "t_b"]
    await store.record_paused_thread("t_b")  # paused again: the clock restarts
    assert await store.stale_paused_threads(24 * 3600) == ["t_a"]
    await store.forget_paused_thread("t_a")
    assert await store.stale_paused_threads(-1) == ["t_b"]


async def test_close_waits_for_in_flight_queries(tmp_path: Path) -> None:
    import asyncio

    s = SQLiteStore(str(tmp_path / "race.db"))
    await s.init()
    tasks = [asyncio.create_task(s.incr_daily("k")) for _ in range(20)]
    await asyncio.sleep(0)
    for t in tasks[:10]:
        t.cancel()  # cancelled callers leave their worker threads running
    await s.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    with pytest.raises(RuntimeError):
        await s.get_plan("x")
