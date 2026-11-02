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
