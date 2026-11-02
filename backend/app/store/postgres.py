"""Postgres implementation of the Store protocol (psycopg 3 async + psycopg_pool).

Same tables and semantics as the SQLite store (sliding two-window rate limits, retention) with JSONB and
TIMESTAMPTZ columns. Counters use single-statement upserts with ``RETURNING`` so concurrent hits are counted
atomically; a denied hit is compensated with a decrement, so concurrent hits never admit more than the limit.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.store.base import RunRecord
from app.store.sqlite import (
    DAILY_COUNTER_TTL_DAYS,
    JSON_RUN_COLUMNS,
    RATE_LIMIT_ROW_TTL_S,
    RUN_COLUMNS,
    sliding_estimate,
    sliding_retry_after,
    utc_now,
    window_bounds,
)

Clock = Callable[[], datetime]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS plans (
        share_id   TEXT PRIMARY KEY,
        record     JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS plans_expires_at ON plans (expires_at)",
    """CREATE TABLE IF NOT EXISTS feedback (
        id         BIGSERIAL PRIMARY KEY,
        share_id   TEXT NOT NULL,
        rating     TEXT NOT NULL,
        reason     TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id          TEXT PRIMARY KEY,
        created_at      TIMESTAMPTZ NOT NULL,
        git_sha         TEXT NOT NULL,
        model           TEXT,
        status          TEXT NOT NULL,
        mode            TEXT,
        latency_ms      INTEGER,
        input_tokens    INTEGER NOT NULL DEFAULT 0,
        output_tokens   INTEGER NOT NULL DEFAULT 0,
        cost_usd        DOUBLE PRECISION,
        tool_calls      JSONB NOT NULL DEFAULT '{}'::jsonb,
        violation_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
        submit_attempts INTEGER NOT NULL DEFAULT 0,
        degradations    JSONB NOT NULL DEFAULT '[]'::jsonb
    )""",
    "CREATE INDEX IF NOT EXISTS runs_created_at ON runs (created_at)",
    """CREATE TABLE IF NOT EXISTS rate_limits (
        key          TEXT NOT NULL,
        window_start BIGINT NOT NULL,
        count        INTEGER NOT NULL,
        PRIMARY KEY (key, window_start)
    )""",
    """CREATE TABLE IF NOT EXISTS daily_counters (
        key   TEXT NOT NULL,
        day   DATE NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (key, day)
    )""",
    """CREATE TABLE IF NOT EXISTS eval_summary (
        id         BIGSERIAL PRIMARY KEY,
        summary    JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS paused_threads (
        thread_id  TEXT PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL
    )""",
]


def normalize_pg_url(url: str) -> str:
    """Accept ``postgres://``, ``postgresql://`` and SQLAlchemy-style ``postgresql+psycopg://`` URLs."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


class PostgresStore:
    def __init__(self, url: str, clock: Clock | None = None, min_size: int = 1, max_size: int = 5) -> None:
        self.url = normalize_pg_url(url)
        self._clock: Clock = clock or utc_now
        self._pool: Any = None
        self._min_size = min_size
        self._max_size = max_size

    async def init(self) -> None:
        if self._pool is not None:
            return
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            self.url, min_size=self._min_size, max_size=self._max_size, open=False,
            kwargs={"autocommit": True, "prepare_threshold": None}, name="app-store",
        )
        await pool.open(wait=True, timeout=15)
        self._pool = pool
        async with pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    def _now(self) -> datetime:
        return self._clock()

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if self._pool is None:
            raise RuntimeError("PostgresStore.init() was not called")
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            if cur.description is None:
                return []
            return list(await cur.fetchall())

    async def _rowcount(self, sql: str, params: tuple[Any, ...]) -> int:
        if self._pool is None:
            raise RuntimeError("PostgresStore.init() was not called")
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return max(0, int(cur.rowcount))

    async def ping(self) -> bool:
        rows = await self._execute("SELECT 1")
        return bool(rows) and rows[0][0] == 1

    async def save_plan(self, share_id: str, record: dict[str, Any], ttl_days: int = 30) -> None:
        from psycopg.types.json import Jsonb

        now = self._now()
        await self._execute(
            "INSERT INTO plans (share_id, record, created_at, expires_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (share_id) DO UPDATE SET record = EXCLUDED.record, created_at = EXCLUDED.created_at, "
            "expires_at = EXCLUDED.expires_at",
            (share_id, Jsonb(record), now, now + timedelta(days=ttl_days)),
        )

    async def get_plan(self, share_id: str) -> dict[str, Any] | None:
        rows = await self._execute("SELECT record FROM plans WHERE share_id = %s AND expires_at > %s",
                                   (share_id, self._now()))
        return dict(rows[0][0]) if rows else None

    async def save_feedback(self, share_id: str, rating: str, reason: str | None) -> None:
        await self._execute("INSERT INTO feedback (share_id, rating, reason, created_at) VALUES (%s, %s, %s, %s)",
                            (share_id, rating, reason, self._now()))

    async def list_feedback(self, share_id: str) -> list[dict[str, Any]]:
        rows = await self._execute("SELECT rating, reason FROM feedback WHERE share_id = %s ORDER BY id",
                                   (share_id,))
        return [{"rating": r[0], "reason": r[1]} for r in rows]

    async def record_run(self, run: RunRecord) -> None:
        from psycopg.types.json import Jsonb

        values = [Jsonb(getattr(run, c)) if c in JSON_RUN_COLUMNS else getattr(run, c) for c in RUN_COLUMNS]
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in RUN_COLUMNS if c != "run_id")
        await self._execute(
            f"INSERT INTO runs ({', '.join(RUN_COLUMNS)}) VALUES ({', '.join('%s' for _ in RUN_COLUMNS)}) "
            f"ON CONFLICT (run_id) DO UPDATE SET {updates}",
            tuple(values),
        )

    async def get_run(self, run_id: str) -> RunRecord | None:
        rows = await self._execute(f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE run_id = %s", (run_id,))
        if not rows:
            return None
        return RunRecord.model_validate(dict(zip(RUN_COLUMNS, rows[0], strict=True)))

    async def hit_rate_limit(self, key: str, window_s: int, limit: int) -> tuple[bool, int]:
        start, prev_start, elapsed = window_bounds(self._now().timestamp(), window_s)
        rows = await self._execute(
            "INSERT INTO rate_limits (key, window_start, count) VALUES (%s, %s, 1) "
            "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + 1 RETURNING count",
            (key, start),
        )
        current = int(rows[0][0])
        prev_rows = await self._execute("SELECT count FROM rate_limits WHERE key = %s AND window_start = %s",
                                        (key, prev_start))
        previous = int(prev_rows[0][0]) if prev_rows else 0
        if sliding_estimate(previous, current, elapsed, window_s) <= limit + 1e-9:
            return True, 0
        await self._execute("UPDATE rate_limits SET count = count - 1 WHERE key = %s AND window_start = %s",
                            (key, start))
        return False, sliding_retry_after(previous, current - 1, limit, elapsed, window_s)

    async def incr_daily(self, key: str) -> int:
        rows = await self._execute(
            "INSERT INTO daily_counters (key, day, count) VALUES (%s, %s, 1) "
            "ON CONFLICT (key, day) DO UPDATE SET count = daily_counters.count + 1 RETURNING count",
            (key, self._now().astimezone(UTC).date()),
        )
        return int(rows[0][0])

    async def save_eval_summary(self, summary: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        await self._execute("INSERT INTO eval_summary (summary, created_at) VALUES (%s, %s)",
                            (Jsonb(summary), self._now()))

    async def get_eval_summary(self) -> dict[str, Any] | None:
        rows = await self._execute("SELECT summary FROM eval_summary ORDER BY id DESC LIMIT 1")
        return dict(rows[0][0]) if rows else None

    async def record_paused_thread(self, thread_id: str) -> None:
        await self._execute(
            "INSERT INTO paused_threads (thread_id, created_at) VALUES (%s, %s) "
            "ON CONFLICT (thread_id) DO UPDATE SET created_at = EXCLUDED.created_at",
            (thread_id, self._now()),
        )

    async def forget_paused_thread(self, thread_id: str) -> None:
        await self._execute("DELETE FROM paused_threads WHERE thread_id = %s", (thread_id,))

    async def stale_paused_threads(self, max_age_s: int) -> list[str]:
        cutoff = self._now() - timedelta(seconds=max_age_s)
        rows = await self._execute("SELECT thread_id FROM paused_threads WHERE created_at < %s ORDER BY created_at",
                                   (cutoff,))
        return [str(r[0]) for r in rows]

    async def purge_expired(self, run_retention_days: int, feedback_retention_days: int) -> dict[str, int]:
        now = self._now()
        day_cutoff = (now - timedelta(days=DAILY_COUNTER_TTL_DAYS)).astimezone(UTC).date()
        return {
            "plans": await self._rowcount("DELETE FROM plans WHERE expires_at <= %s", (now,)),
            "runs": await self._rowcount("DELETE FROM runs WHERE created_at < %s",
                                         (now - timedelta(days=run_retention_days),)),
            "feedback": await self._rowcount("DELETE FROM feedback WHERE created_at < %s",
                                             (now - timedelta(days=feedback_retention_days),)),
            "rate_limits": await self._rowcount("DELETE FROM rate_limits WHERE window_start < %s",
                                                (int(now.timestamp()) - RATE_LIMIT_ROW_TTL_S,)),
            "daily_counters": await self._rowcount("DELETE FROM daily_counters WHERE day < %s", (day_cutoff,)),
        }
