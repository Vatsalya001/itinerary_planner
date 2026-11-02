"""Server-sent events framing and HTML-safe JSON.

JSON bodies and SSE frames escape ``<``, ``>``, ``&`` and the JS line separators as ``\\uXXXX``. The result is still
valid JSON that parses to the same strings, but user-supplied markup (``<img onerror=...>``) can never appear
literally in a response body, so a mis-sniffed or mis-rendered response cannot execute it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from starlette.responses import JSONResponse

from app.domain.schemas import TraceEvent

# Seconds of silence before a keep-alive is sent (patched to a tiny value in tests).
HEARTBEAT_INTERVAL_S: float = 10.0
# Keep-alives are SSE comment lines, not TraceEvents, so every TraceEvent in a stream has a unique ``seq``.
# SSE clients (and our frontend parser) ignore comment lines.
KEEPALIVE_FRAME = b": keep-alive\n\n"

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

_UNSAFE = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "\u2028": "\\u2028", "\u2029": "\\u2029"}
_UNSAFE_TABLE = str.maketrans(_UNSAFE)


def safe_json_dumps(obj: Any) -> str:
    """Compact JSON with HTML-significant characters escaped (they can only occur inside JSON strings)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).translate(_UNSAFE_TABLE)


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return safe_json_dumps(content).encode("utf-8")


def format_event(event: TraceEvent) -> bytes:
    """One SSE frame: ``data: <json>\\n\\n``. The JSON never contains a raw newline."""
    return f"data: {safe_json_dumps(event.model_dump(mode='json'))}\n\n".encode()


class StreamEnd:
    """Queue sentinel: the producer is done."""


STREAM_END = StreamEnd()


async def event_stream(queue: asyncio.Queue[TraceEvent | StreamEnd]) -> AsyncIterator[bytes]:
    """Forward queued events as SSE frames, with a ``: keep-alive`` comment after every HEARTBEAT_INTERVAL_S of
    silence.

    Stops after the sentinel. If the client disconnects, Starlette stops iterating; the producer task is
    independent and keeps running (its events are simply never read).
    """
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_S)
        except TimeoutError:
            yield KEEPALIVE_FRAME
            continue
        if isinstance(item, StreamEnd):
            return
        yield format_event(item)
