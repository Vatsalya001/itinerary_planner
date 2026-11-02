"""Storage interface (plans for share links, run log, feedback, rate limits, eval results).

OWNER: API agent (Phase 2) implements SQLite and Postgres versions. Contract only.

Rate limits are sliding two-window estimates (``previous * (1 - elapsed/window) + current`` over windows aligned to
multiples of ``window_s``), not fixed windows. A request that is refused is never counted.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

# (key, window_s, limit) for one sliding rate-limit window
RateWindow = tuple[str, int, int]
# (allowed, retry_after_seconds, key of the limit that refused: a window key or the daily key; None when allowed)
LimitDecision = tuple[bool, int, str | None]


class RunRecord(BaseModel):
    run_id: str
    created_at: datetime
    git_sha: str
    model: str | None
    status: str
    mode: str | None
    latency_ms: int | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    tool_calls: dict[str, int] = Field(default_factory=dict)
    violation_codes: list[str] = Field(default_factory=list)
    submit_attempts: int = 0
    degradations: list[str] = Field(default_factory=list)
    # never store raw free text, mood or constraint text here


class Store(Protocol):
    async def init(self) -> None: ...
    async def close(self) -> None: ...
    async def ping(self) -> bool: ...  # health check

    async def save_plan(self, share_id: str, record: dict[str, Any], ttl_days: int = 30) -> None: ...
    async def get_plan(self, share_id: str) -> dict[str, Any] | None: ...  # None when unknown or expired
    async def save_feedback(self, share_id: str, rating: str, reason: str | None) -> None: ...

    async def record_run(self, run: RunRecord) -> None: ...

    async def hit_rate_limit(self, key: str, window_s: int, limit: int) -> tuple[bool, int]:
        """Count one hit if it fits; return (allowed, retry_after_seconds). Sliding two-window estimate; a denied
        hit is not counted."""
        ...

    async def hit_limits(self, windows: Sequence[RateWindow], daily_key: str | None = None,
                         daily_cap: int | None = None) -> LimitDecision:
        """Check every window (and the UTC-day counter ``daily_key`` against ``daily_cap``) first, then count the
        request in all of them only if all allow it, atomically. When several refuse, ``retry_after`` is the
        longest wait and the key is that limit's; the daily cap's wait is the time to UTC midnight."""
        ...

    async def incr_daily(self, key: str) -> int:
        """Increment and return today's (UTC) counter for ``key`` (global plan cap)."""
        ...

    async def save_eval_summary(self, summary: dict[str, Any]) -> None: ...
    async def get_eval_summary(self) -> dict[str, Any] | None: ...

    # Retention (app.store.retention runs these hourly)
    async def record_paused_thread(self, thread_id: str) -> None: ...
    async def forget_paused_thread(self, thread_id: str) -> None: ...
    async def stale_paused_threads(self, max_age_s: int) -> list[str]: ...
    async def purge_expired(self, run_retention_days: int, feedback_retention_days: int) -> dict[str, int]: ...
