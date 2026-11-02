"""Client-side sliding-window rate limiting per model (configured in models.yaml as ``rate_limit``).

Free tiers such as Cerebras' trial allow N requests per ROLLING window (5 per 60 s): after 5 calls the 6th gets a
429 with retry-after ~58 s. A token bucket (langchain's InMemoryRateLimiter) allows bursts that break such a
window, so this limiter books each request at the earliest time that keeps every 60 s window at N or fewer.

* One limiter per model ID, shared by every run in the process, attached to the chat model as LangChain's
  ``rate_limiter`` so it gates every call through that model (parse, agent, judge).
* Each model call may run under a budget (``call_budget``): when the wait needed for a slot would not leave
  enough of that budget for the call itself, ``RateLimitWaitError`` (a 429-like error) is raised at once, so
  ``with_fallbacks`` moves straight to the next model and the failure is classified as a rate limit.
* Time spent waiting is appended to the budget's sink so the run can report ``rate_limit_wait_ms``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.rate_limiters import BaseRateLimiter

# Seconds a call still needs after its slot opens; a wait that would leave less than this fails fast instead.
MIN_CALL_S = 2.0

_DEADLINE: ContextVar[float | None] = ContextVar("llm_call_deadline", default=None)  # time.monotonic()
_WAIT_SINK: ContextVar[list[float] | None] = ContextVar("llm_rate_limit_waits", default=None)


class RateLimitWaitError(RuntimeError):
    """The model's request window is full for longer than this call can wait. Looks like an HTTP 429."""

    status_code = 429

    def __init__(self, name: str, wait_s: float, budget_s: float) -> None:
        super().__init__(f"Rate limit reached for {name}: next request slot in {wait_s:.1f}s, "
                         f"but only {max(0.0, budget_s):.1f}s left for this call")
        self.wait_s = wait_s


@contextmanager
def call_budget(seconds: float | None, waits: list[float] | None = None) -> Iterator[None]:
    """Run model calls inside this block under a time budget, recording rate-limit waits in ``waits``.
    Nested budgets can only shorten the deadline (e.g. the primary's share of a turn)."""
    deadline = None if seconds is None else time.monotonic() + max(0.0, seconds)
    outer = _DEADLINE.get()
    if outer is not None and (deadline is None or outer < deadline):
        deadline = outer
    deadline_token = _DEADLINE.set(deadline)
    sink_token = _WAIT_SINK.set(waits) if waits is not None else None
    try:
        yield
    finally:
        _DEADLINE.reset(deadline_token)
        if sink_token is not None:
            _WAIT_SINK.reset(sink_token)


class SlidingWindowRateLimiter(BaseRateLimiter):
    """At most ``requests`` calls in any ``window_s`` seconds (process-wide, thread-safe bookkeeping)."""

    def __init__(self, requests: int, window_s: float, name: str = "model") -> None:
        if requests < 1 or window_s <= 0:
            raise ValueError("rate_limit needs requests >= 1 and window_s > 0")
        self.requests = requests
        self.window_s = float(window_s)
        self.name = name
        self._slots: deque[float] = deque()  # monotonic start times of granted (or booked) requests
        self._lock = threading.Lock()

    def _book(self) -> float:
        """Book the earliest allowed slot and return how long to wait for it. Raises when the call's budget
        can't cover the wait (nothing is booked then)."""
        now = time.monotonic()
        with self._lock:
            while self._slots and self._slots[0] <= now - self.window_s:
                self._slots.popleft()
            slot = now
            if len(self._slots) >= self.requests:
                slot = max(now, self._slots[-self.requests] + self.window_s)
            wait = slot - now
            deadline = _DEADLINE.get()
            if wait > 0 and deadline is not None and now + wait > deadline - MIN_CALL_S:
                raise RateLimitWaitError(self.name, wait, deadline - now)
            self._slots.append(slot)
        if wait > 0:
            sink = _WAIT_SINK.get()
            if sink is not None:
                sink.append(wait)
        return wait

    def acquire(self, *, blocking: bool = True) -> bool:
        wait = self._book()
        if wait > 0:
            time.sleep(wait)
        return True

    async def aacquire(self, *, blocking: bool = True) -> bool:
        wait = self._book()
        if wait > 0:
            await asyncio.sleep(wait)
        return True

    def reset(self) -> None:
        with self._lock:
            self._slots.clear()


_LIMITERS: dict[str, SlidingWindowRateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(model_id: str, requests: int, window_s: float) -> SlidingWindowRateLimiter:
    """The process-wide limiter for a model (recreated only if its configuration changes)."""
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(model_id)
        if limiter is None or (limiter.requests, limiter.window_s) != (requests, float(window_s)):
            limiter = SlidingWindowRateLimiter(requests, window_s, name=model_id)
            _LIMITERS[model_id] = limiter
        return limiter


def reset_limiters() -> None:
    with _LIMITERS_LOCK:
        _LIMITERS.clear()
