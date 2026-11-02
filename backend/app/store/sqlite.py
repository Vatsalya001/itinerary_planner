"""SQLite implementation of the Store protocol (stdlib ``sqlite3``, run in a worker thread).

One connection guarded by a lock; WAL mode for file databases. Timestamps are stored as UTC epoch seconds.

Rate limits use a two-window sliding estimate (``previous * (1 - elapsed/window) + current``) over windows aligned
to multiples of ``window_s``. That avoids the 2x burst a plain fixed window allows across a boundary. Denied hits
are not counted, so a client retrying after a 429 doesn't extend its own lockout.

Retention (``purge_expired``, ``stale_paused_threads``) is driven by ``app.store.retention``.
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
CREATE INDEX IF NOT EXISTS runs_created_at ON runs (created_at);
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
CREATE TABLE IF NOT EXISTS paused_threads (
    thread_id  TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
"""

RUN_COLUMNS = (
    "run_id", "created_at", "git_sha", "model", "status", "mode", "latency_ms", "input_tokens", "output_tokens",
    "cost_usd", "tool_calls", "violation_codes", "submit_attempts", "degradations",
)
JSON_RUN_COLUMNS = {"tool_calls", "violation_codes", "degradations"}
RATE_LIMIT_ROW_TTL_S = 2 * 86400  # longest window is a day; the previous window is still needed
DAILY_COUNTER_TTL_DAYS = 7


def utc_now() -> datetime:
    return datetime.now(UTC)


def window_bounds(now_ts: float, window_s: int) -> tuple[int, int, float]:
    """(current window start, previous window start, seconds elapsed in the current window)."""
    start = int(now_ts // window_s) * window_s
    return start, start - window_s, now_ts - start


def sliding_estimate(previous: int, current: int, elapsed: float, window_s: int) -> float:
    return previous * max(0.0, 1.0 - elapsed / window_s) + current


def sliding_retry_after(previous: int, current: int, limit: int, elapsed: float, window_s: int) -> int:
    """Seconds until one more hit fits, assuming no other hits. ``current`` excludes the denied hit."""
    remaining = window_s - elapsed
    if current + 1 <= limit and previous > 0:
        # Wait inside this window until the previous window's weight has decayed enough.
        wait = window_s * (1.0 - (limit - current - 1) / previous) - elapsed
        if wait <= remaining:
            return max(1, math.ceil(wait))
    if current == 0:
        return max(1, math.ceil(remaining))
    # Otherwise wait into the next window, where this window's hits become the decaying "previous".
    into_next = min(float(window_s), max(0.0, window_s * (1.0 - (limit - 1) / current)))
    return max(1, math.ceil(remaining + into_next))


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
        # Under the lock: a cancelled caller's worker thread may still be mid-query on this connection.
        def _close() -> None:
            with self._lock:
                conn, self._conn = self._conn, None
                if conn is not None:
                    conn.close()

        await asyncio.to_thread(_close)

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._conn is None:
            raise RuntimeError("SQLiteStore is not open (init() not called, or closed)")

        def call() -> T:
            with self._lock:
                conn = self._conn
                if conn is None:
                    raise RuntimeError("SQLiteStore was closed")
                return fn(conn)

        return await asyncio.to_thread(call)

    def _now(self) -> datetime:
        return self._clock()

    async def ping(self) -> bool:
        return await self._run(lambda c: c.execute("SELECT 1").fetchone()[0] == 1)

    # -- plans and feedback -------------------------------------------------------------------------------

    async def save_plan(self, share_id: str, record: dict[str, Any], ttl_days: int = 30) -> None:
        now = self._now()
        payload = json.dumps(record, ensure_ascii=False)
        created, expires = now.timestamp(), (now + timedelta(days=ttl_days)).timestamp()
        await self._run(lambda c: c.execute(
            "INSERT INTO plans (share_id, record, created_at, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (share_id) DO UPDATE SET record = excluded.record, created_at = excluded.created_at, "
            "expires_at = excluded.expires_at",
            (share_id, payload, created, expires),
        ))

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
        start, prev_start, elapsed = window_bounds(now_ts, window_s)

        def op(c: sqlite3.Connection) -> tuple[bool, int]:
            current = int(c.execute(
                "INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT (key, window_start) DO UPDATE SET count = count + 1 RETURNING count",
                (key, start),
            ).fetchone()[0])
            row = c.execute("SELECT count FROM rate_limits WHERE key = ? AND window_start = ?",
                            (key, prev_start)).fetchone()
            previous = int(row[0]) if row else 0
            if sliding_estimate(previous, current, elapsed, window_s) <= limit + 1e-9:
                return True, 0
            c.execute("UPDATE rate_limits SET count = count - 1 WHERE key = ? AND window_start = ?", (key, start))
            return False, sliding_retry_after(previous, current - 1, limit, elapsed, window_s)

        return await self._run(op)

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

    # -- retention ----------------------------------------------------------------------------------------

    async def record_paused_thread(self, thread_id: str) -> None:
        """Remember a thread paused at a clarification so its checkpoint can be expired if abandoned."""
        now_ts = self._now().timestamp()
        await self._run(lambda c: c.execute(
            "INSERT INTO paused_threads (thread_id, created_at) VALUES (?, ?) "
            "ON CONFLICT (thread_id) DO UPDATE SET created_at = excluded.created_at",
            (thread_id, now_ts),
        ))

    async def forget_paused_thread(self, thread_id: str) -> None:
        await self._run(lambda c: c.execute("DELETE FROM paused_threads WHERE thread_id = ?", (thread_id,)))

    async def stale_paused_threads(self, max_age_s: int) -> list[str]:
        cutoff = self._now().timestamp() - max_age_s

        def op(c: sqlite3.Connection) -> list[str]:
            rows = c.execute("SELECT thread_id FROM paused_threads WHERE created_at < ? ORDER BY created_at",
                             (cutoff,)).fetchall()
            return [str(r[0]) for r in rows]

        return await self._run(op)

    async def purge_expired(self, run_retention_days: int, feedback_retention_days: int) -> dict[str, int]:
        """Delete expired plans, old runs and feedback, stale rate-limit windows and day counters."""
        now = self._now()
        now_ts = now.timestamp()
        runs_cutoff = (now - timedelta(days=run_retention_days)).timestamp()
        feedback_cutoff = (now - timedelta(days=feedback_retention_days)).timestamp()
        day_cutoff = utc_day(now - timedelta(days=DAILY_COUNTER_TTL_DAYS))

        def op(c: sqlite3.Connection) -> dict[str, int]:
            return {
                "plans": c.execute("DELETE FROM plans WHERE expires_at <= ?", (now_ts,)).rowcount,
                "runs": c.execute("DELETE FROM runs WHERE created_at < ?", (runs_cutoff,)).rowcount,
                "feedback": c.execute("DELETE FROM feedback WHERE created_at < ?", (feedback_cutoff,)).rowcount,
                "rate_limits": c.execute("DELETE FROM rate_limits WHERE window_start < ?",
                                         (int(now_ts) - RATE_LIMIT_ROW_TTL_S,)).rowcount,
                "daily_counters": c.execute("DELETE FROM daily_counters WHERE day < ?", (day_cutoff,)).rowcount,
            }

        return await self._run(op)
