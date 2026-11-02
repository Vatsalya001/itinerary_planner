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
from app.store.sqlite import SQLiteStore, fixed_window, seconds_until_utc_midnight, sqlite_path_from_url
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


async def test_rate_limit_fixed_windows(store: SQLiteStore, clock: MutableClock) -> None:
    key = "ip:203.0.113.9:10m"
    assert await store.hit_rate_limit(key, 600, 2) == (True, 0)
    assert await store.hit_rate_limit(key, 600, 2) == (True, 0)
    assert await store.hit_rate_limit(key, 600, 2) == (False, 300)
    clock.now = START + timedelta(minutes=4, seconds=30)
    assert await store.hit_rate_limit(key, 600, 2) == (False, 30)
    assert await store.hit_rate_limit("ip:198.51.100.1:10m", 600, 2) == (True, 0)  # keys are independent

    clock.now = START + timedelta(minutes=5)  # next window
    assert await store.hit_rate_limit(key, 600, 2) == (True, 0)


async def test_rate_limit_day_window(store: SQLiteStore, clock: MutableClock) -> None:
    key = "ip:203.0.113.9:day"
    assert (await store.hit_rate_limit(key, 86400, 1))[0] is True
    allowed, retry = await store.hit_rate_limit(key, 86400, 1)
    assert allowed is False and retry == 11 * 3600 + 55 * 60
    clock.now = datetime(2026, 11, 6, 0, 0, 1, tzinfo=UTC)
    assert (await store.hit_rate_limit(key, 86400, 1))[0] is True


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
    assert fixed_window(START.timestamp(), 600) == (int(START.timestamp()) - 300, 300)
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
    assert checkpoint_path_for("sqlite:///./data/app.db") == "data/checkpoints.db"


async def test_open_checkpointer_memory_and_sqlite(tmp_path: Path) -> None:
    async with open_checkpointer("sqlite:///:memory:") as saver:
        assert isinstance(saver, InMemorySaver)
    async with open_checkpointer(f"sqlite:///{tmp_path}/sub/app.db") as saver:
        assert isinstance(saver, AsyncSqliteSaver)
        assert (tmp_path / "sub" / "checkpoints.db").exists()
        assert await saver.aget_tuple({"configurable": {"thread_id": "nope", "checkpoint_ns": ""}}) is None
