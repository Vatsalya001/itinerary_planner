"""Trace events: one emitter shared by the baseline pipeline and the LangGraph nodes.

Events describe what actually ran (node/tool names, durations, counts). They never carry secrets, raw
provider payloads or the model's private reasoning; the only model text is the tool-call ``reason``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.domain.schemas import TraceEvent, TraceType

Emit = Callable[[TraceEvent], None]

_MAX_TEXT = 240


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


class Tracer:
    def __init__(self, run_id: str, emit: Emit | None = None, start_seq: int = 0) -> None:
        self.run_id = run_id
        self._emit = emit
        self._seq = start_seq
        self.events: list[TraceEvent] = []

    def bind(self, emit: Emit | None) -> None:
        """Point the tracer at a new sink (graph nodes re-bind to the current stream writer)."""
        self._emit = emit

    @property
    def seq(self) -> int:
        return self._seq

    def emit(self, type_: TraceType, **fields: Any) -> TraceEvent:
        self._seq += 1
        for key in ("summary", "reason"):
            if key in fields:
                fields[key] = _clip(fields[key])
        event = TraceEvent(type=type_, run_id=self.run_id, seq=self._seq, ts=datetime.now(UTC), **fields)
        self.events.append(event)
        if self._emit is not None:
            self._emit(event)
        return event

    @contextmanager
    def node(self, name: str) -> Iterator[dict[str, Any]]:
        """Emit node_started/node_completed around a block. Set ``info["summary"]`` inside the block."""
        info: dict[str, Any] = {}
        self.emit("node_started", node=name)
        t0 = time.perf_counter()
        try:
            yield info
        finally:
            self.emit(
                "node_completed",
                node=name,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                summary=info.get("summary"),
                data=info.get("data"),
            )

    @contextmanager
    def tool(self, name: str, reason: str | None = None, node: str = "tools") -> Iterator[dict[str, Any]]:
        """Emit tool_called then tool_completed (or tool_failed if the block raises; the error propagates)."""
        info: dict[str, Any] = {}
        self.emit("tool_called", node=node, tool=name, reason=reason)
        t0 = time.perf_counter()
        try:
            yield info
        except Exception as exc:
            self.emit(
                "tool_failed",
                node=node,
                tool=name,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                summary=info.get("summary") or f"{type(exc).__name__}",
                data={"code": getattr(exc, "code", type(exc).__name__), "recovered": info.get("recovered", False)},
            )
            raise
        self.emit(
            "tool_completed",
            node=node,
            tool=name,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            summary=info.get("summary"),
            data=info.get("data"),
        )


def new_run_id() -> str:
    import secrets

    return "r_" + secrets.token_hex(6)


def monotonic_ms() -> int:
    return int(time.perf_counter() * 1000)
