"""Data retention: a sweep on startup and then hourly (started and cancelled by the app lifespan).

* Expired share-link plans (``settings.share_ttl_days``, default 30: also Google's limit for coordinates).
* Run log rows older than ``RUN_RETENTION_DAYS`` (90, IMPLEMENTATION_PLAN.md section 11).
* Feedback older than ``FEEDBACK_RETENTION_DAYS`` (90; ``reason`` is free text).
* Checkpoints of clarification threads abandoned for more than ``PAUSED_THREAD_MAX_AGE_S`` (24 h). Paused
  checkpoints hold the raw request, so they must not linger. The API records each thread that pauses at
  ``needs_clarification`` (``record_paused_thread``) and forgets it when a resume reaches a terminal status. The
  sweep deletes stale ones with ``checkpointer.adelete_thread``.
* Old rate-limit windows and daily counters.

Logs carry counts only, never IDs of users or any user text.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("app.retention")

RUN_RETENTION_DAYS = 90
FEEDBACK_RETENTION_DAYS = 90
PAUSED_THREAD_MAX_AGE_S = 24 * 3600
SWEEP_INTERVAL_S: float = 3600.0


async def sweep(store: Any, checkpointer: Any) -> dict[str, int]:
    """One retention pass. Stores without the retention methods are skipped. Returns deletion counts."""
    counts: dict[str, int] = {}
    if store is None:
        return counts
    purge = getattr(store, "purge_expired", None)
    if purge is not None:
        counts.update(await purge(run_retention_days=RUN_RETENTION_DAYS,
                                  feedback_retention_days=FEEDBACK_RETENTION_DAYS))
    stale = getattr(store, "stale_paused_threads", None)
    if stale is not None and checkpointer is not None:
        deleted = failed = 0
        for thread_id in await stale(PAUSED_THREAD_MAX_AGE_S):
            try:
                await checkpointer.adelete_thread(thread_id)
            except Exception as exc:
                failed += 1
                log.warning("checkpoint delete failed", extra={"error_type": type(exc).__name__})
                continue  # keep the row; the next sweep retries
            await store.forget_paused_thread(thread_id)
            deleted += 1
        counts["paused_threads"] = deleted
        if failed:
            counts["paused_threads_failed"] = failed
    if checkpointer is not None and hasattr(checkpointer, "alist"):
        now = store._clock() if hasattr(store, "_clock") else datetime.now(UTC)
        paused = set(await store.stale_paused_threads(-1)) if hasattr(store, "stale_paused_threads") else set()
        latest: dict[str, datetime] = {}
        async for entry in checkpointer.alist(None, limit=1000):
            try:
                stamp = datetime.fromisoformat(entry.checkpoint.get("ts"))
                thread_id = entry.config.get("configurable", {}).get("thread_id")
                if thread_id and thread_id not in paused:
                    latest[thread_id] = max(latest.get(thread_id, stamp), stamp)
            except (TypeError, ValueError):
                continue
        orphaned = [tid for tid, stamp in latest.items() if (now - stamp).total_seconds() > PAUSED_THREAD_MAX_AGE_S]
        for thread_id in orphaned:
            await checkpointer.adelete_thread(thread_id)
        if orphaned:
            counts["orphaned_threads"] = len(orphaned)
    log.info("retention sweep", extra={f"deleted_{k}": v for k, v in counts.items()})
    return counts


async def retention_loop(store: Any, checkpointer: Any, interval_s: float | None = None) -> None:
    """Sweep now, then every ``interval_s`` seconds until cancelled. Errors are logged, never raised."""
    while True:
        try:
            await sweep(store, checkpointer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("retention sweep failed", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(SWEEP_INTERVAL_S if interval_s is None else interval_s)
