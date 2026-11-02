"""SQLite implementation of the Store protocol (stdlib ``sqlite3``, run in a worker thread).

One connection guarded by a lock; WAL mode for file databases. Timestamps are stored as UTC epoch seconds.
Rate limits use fixed windows aligned to multiples of ``window_s`` since the epoch.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from app.store.base import RunRecord

T = TypeVar("T")
Clock = Callable[[], datetime]

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    share_id   TEXT PRIMARY KEY,
    record     TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS plans_expires_at ON plans (expires_at);
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    share_id   TEXT NOT NULL,
    rating     TEXT NOT NULL,
    reason     TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    git_sha         TEXT NOT NULL,
    model           TEXT,
    status          TEXT NOT NULL,
    mode            TEXT,
    latency_ms      INTEGER,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,
    tool_calls      TEXT NOT NULL DEFAULT '{}',
    violation_codes TEXT NOT NULL DEFAULT '[]',
    submit_attempts INTEGER NOT NULL DEFAULT 0,
    degradations    TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS rate_limits (
    key          TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);
CREATE TABLE IF NOT EXISTS daily_counters (
    key   TEXT NOT NULL,
    day   TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (key, day)
);
CREATE TABLE IF NOT EXISTS eval_summary (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    summary    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

RUN_COLUMNS = (
    "run_id", "created_at", "git_sha", "model", "status", "mode", "latency_ms", "input_tokens", "output_tokens",
    "cost_usd", "tool_calls", "violation_codes", "submit_attempts", "degradations",
)
JSON_RUN_COLUMNS = {"tool_calls", "violation_codes", "degradations"}
CLEANUP_EVERY = 200  # opportunistic cleanup of old rate-limit windows and expired plans


def utc_now() -> datetime:
    return datetime.now(UTC)


def fixed_window(now_ts: float, window_s: int) -> tuple[int, int]:
    """(window_start, seconds until the window resets, at least 1)."""
    start = int(now_ts // window_s) * window_s
    return start, max(1, math.ceil(start + window_s - now_ts))


def utc_day(now: datetime) -> str:
    return now.astimezone(UTC).date().isoformat()


def seconds_until_utc_midnight(now: datetime) -> int:
    now = now.astimezone(UTC)
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    return max(1, math.ceil((midnight - now).total_seconds()))


def sqlite_path_from_url(url: str) -> str:
    """``sqlite:///:memory:`` -> ``:memory:``; ``sqlite:///./data/app.db`` -> ``./data/app.db``;
    ``sqlite:////abs/app.db`` -> ``/abs/app.db``."""
    if not url.startswith("sqlite://"):
        raise ValueError("not a sqlite URL")
    rest = url[len("sqlite://"):]
    if rest.startswith("/"):
        rest = rest[1:]
    return rest or ":memory:"


class SQLiteStore:
    def __init__(self, path: str = ":memory:", clock: Clock | None = None) -> None:
        self.path = path
        self._clock: Clock = clock or utc_now
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._ops = 0

    @property
    def is_memory(self) -> bool:
        return self.path in (":memory:", "") or self.path.startswith("file::memory:")

    # -- plumbing -----------------------------------------------------------------------------------------

    async def init(self) -> None:
        if self._conn is not None:
            return

        def _open() -> sqlite3.Connection:
            if not self.is_memory:
                Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
            conn.row_factory = sqlite3.Row
            if not self.is_memory:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.close)

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        conn = self._conn
        if conn is None:
            raise RuntimeError("SQLiteStore.init() was not called")

        def call() -> T:
            with self._lock:
                return fn(conn)

        return await asyncio.to_thread(call)

    def _now(self) -> datetime:
        return self._clock()

    def _maybe_cleanup(self, conn: sqlite3.Connection, now_ts: float) -> None:
        self._ops += 1
        if self._ops % CLEANUP_EVERY:
            return
        conn.execute("DELETE FROM rate_limits WHERE window_start < ?", (int(now_ts) - 2 * 86400,))
        conn.execute("DELETE FROM plans WHERE expires_at <= ?", (now_ts,))

    async def ping(self) -> bool:
        return await self._run(lambda c: c.execute("SELECT 1").fetchone()[0] == 1)

    # -- plans and feedback -------------------------------------------------------------------------------

    async def save_plan(self, share_id: str, record: dict[str, Any], ttl_days: int = 30) -> None:
        now = self._now()
        payload = json.dumps(record, ensure_ascii=False)
        created, expires = now.timestamp(), (now + timedelta(days=ttl_days)).timestamp()

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO plans (share_id, record, created_at, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (share_id) DO UPDATE SET record = excluded.record, created_at = excluded.created_at, "
                "expires_at = excluded.expires_at",
                (share_id, payload, created, expires),
            )
            self._maybe_cleanup(c, created)

        await self._run(op)

    async def get_plan(self, share_id: str) -> dict[str, Any] | None:
        now_ts = self._now().timestamp()

        def op(c: sqlite3.Connection) -> str | None:
            row = c.execute("SELECT record FROM plans WHERE share_id = ? AND expires_at > ?",
                            (share_id, now_ts)).fetchone()
            return None if row is None else str(row["record"])

        raw = await self._run(op)
        return None if raw is None else dict(json.loads(raw))

    async def save_feedback(self, share_id: str, rating: str, reason: str | None) -> None:
        now_ts = self._now().timestamp()
        await self._run(lambda c: c.execute(
            "INSERT INTO feedback (share_id, rating, reason, created_at) VALUES (?, ?, ?, ?)",
            (share_id, rating, reason, now_ts),
        ))

    async def list_feedback(self, share_id: str) -> list[dict[str, Any]]:
        def op(c: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = c.execute("SELECT rating, reason FROM feedback WHERE share_id = ? ORDER BY id", (share_id,))
            return [dict(r) for r in rows.fetchall()]

        return await self._run(op)

    # -- run log ------------------------------------------------------------------------------------------

    async def record_run(self, run: RunRecord) -> None:
        values: list[Any] = []
        for col in RUN_COLUMNS:
            value = getattr(run, col)
            if col == "created_at":
                value = value.timestamp()
            elif col in JSON_RUN_COLUMNS:
                value = json.dumps(value)
            values.append(value)
        sql = (f"INSERT OR REPLACE INTO runs ({', '.join(RUN_COLUMNS)}) "
               f"VALUES ({', '.join('?' for _ in RUN_COLUMNS)})")
        await self._run(lambda c: c.execute(sql, values))

    async def get_run(self, run_id: str) -> RunRecord | None:
        def op(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute(f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return None if row is None else dict(row)

        row = await self._run(op)
        if row is None:
            return None
        row["created_at"] = datetime.fromtimestamp(row["created_at"], UTC)
        for col in JSON_RUN_COLUMNS:
            row[col] = json.loads(row[col])
        return RunRecord.model_validate(row)

    # -- abuse and spend controls -------------------------------------------------------------------------

    async def hit_rate_limit(self, key: str, window_s: int, limit: int) -> tuple[bool, int]:
        now_ts = self._now().timestamp()
        start, retry_after = fixed_window(now_ts, window_s)

        def op(c: sqlite3.Connection) -> int:
            row = c.execute(
                "INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT (key, window_start) DO UPDATE SET count = count + 1 RETURNING count",
                (key, start),
            ).fetchone()
            self._maybe_cleanup(c, now_ts)
            return int(row[0])

        count = await self._run(op)
        allowed = count <= limit
        return allowed, (0 if allowed else retry_after)

    async def incr_daily(self, key: str) -> int:
        day = utc_day(self._now())

        def op(c: sqlite3.Connection) -> int:
            row = c.execute(
                "INSERT INTO daily_counters (key, day, count) VALUES (?, ?, 1) "
                "ON CONFLICT (key, day) DO UPDATE SET count = count + 1 RETURNING count",
                (key, day),
            ).fetchone()
            return int(row[0])

        return await self._run(op)

    # -- eval summary -------------------------------------------------------------------------------------

    async def save_eval_summary(self, summary: dict[str, Any]) -> None:
        now_ts = self._now().timestamp()
        payload = json.dumps(summary, ensure_ascii=False)
        await self._run(lambda c: c.execute(
            "INSERT INTO eval_summary (summary, created_at) VALUES (?, ?)", (payload, now_ts)))

    async def get_eval_summary(self) -> dict[str, Any] | None:
        def op(c: sqlite3.Connection) -> str | None:
            row = c.execute("SELECT summary FROM eval_summary ORDER BY id DESC LIMIT 1").fetchone()
            return None if row is None else str(row["summary"])

        raw = await self._run(op)
        return None if raw is None else dict(json.loads(raw))
