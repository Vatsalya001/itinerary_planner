"""Storage interface (plans for share links, run log, feedback, rate limits, eval results).

OWNER: API agent (Phase 2) implements SQLite and Postgres versions. Contract only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field


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
        """Count one hit; return (allowed, retry_after_seconds). Fixed window."""
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
