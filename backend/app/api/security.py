"""Pure-ASGI middleware: request IDs, security headers, body-size limit, generic 500s; plus the client-IP helper.

Pure ASGI (not BaseHTTPMiddleware) so streaming responses pass through unbuffered.
Order (outermost first): SecurityHeaders -> ErrorShield -> BodySizeLimit -> app. The 413 and the generic 500
therefore carry the security headers and the request ID too.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import time
from typing import Any

from starlette.requests import ClientDisconnect, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.sse import safe_json_dumps
from app.observability import request_id_var, run_id_var

log = logging.getLogger("app.http")

CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}
_RAW_SECURITY_HEADERS = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]

REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def new_request_id() -> str:
    return "q_" + secrets.token_hex(8)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


async def send_json(send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None
                    ) -> None:
    payload = safe_json_dumps(body).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": raw + (headers or [])})
    await send({"type": "http.response.body", "body": payload, "more_body": False})


class SecurityHeadersMiddleware:
    """Assigns a request ID, adds security headers and X-Request-ID to every HTTP response, logs one access line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, REQUEST_ID_HEADER.encode())
        request_id = incoming if incoming and _REQUEST_ID_RE.match(incoming) else new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        t0 = time.perf_counter()
        status_holder = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                existing = {k.lower() for k, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers += [(k, v) for k, v in _RAW_SECURITY_HEADERS if k not in existing]
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Path only (no query string): share IDs are fine to log, user text never travels in the path.
            log.info("request", extra={
                "method": scope.get("method"), "path": scope.get("path"), "status": status_holder["status"],
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            })
            request_id_var.reset(token)


class ErrorShieldMiddleware:
    """Turns unhandled exceptions into a generic JSON 500 with the request/run ID (never a stack trace)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except ClientDisconnect:
            log.info("client disconnected")
        except Exception as exc:
            log.exception("unhandled error", extra={"error_type": type(exc).__name__})
            _capture(exc)
            if started:
                return  # headers already sent (e.g. mid-stream); nothing more we can say
            body: dict[str, Any] = {"status": "rejected", "detail": "Something went wrong"}
            body["request_id"] = scope.get("state", {}).get("request_id") or request_id_var.get()
            run_id = run_id_var.get()
            if run_id:
                body["run_id"] = run_id
            await send_json(send_wrapper, 500, body)


def _capture(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:  # pragma: no cover - Sentry must never break error handling
        pass


class BodySizeLimitMiddleware:
    """Rejects request bodies over ``max_bytes`` with 413, with or without a Content-Length header.

    Bodies of POST/PUT/PATCH/DELETE requests are read (up to the cap) before the app runs and replayed to it,
    so chunked uploads are capped too. After the replay, ``receive`` is delegated to the server so the app can
    still observe ``http.disconnect`` during a streaming response.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send: Send) -> None:
        await send_json(send, 413, {"status": "rejected", "detail": "Request too large"},
                        headers=[(b"connection", b"close")])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                await send_json(send, 400, {"status": "rejected", "detail": "Invalid Content-Length"})
                return

        chunks: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # client gave up before sending the whole body
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                await self._reject(send)
                return
            chunks.append(body)
            more = bool(message.get("more_body", False))

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _valid_ip(value: str) -> str | None:
    value = value.strip()
    if value.startswith("[") and "]" in value:  # "[2001:db8::1]:443"
        value = value[1:value.index("]")]
    elif value.count(":") == 1:  # "203.0.113.7:51234"
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request: Request, trust_proxy_headers: bool) -> str:
    """The client address used for rate limits.

    Behind one trusted proxy that *appends* the connecting address to X-Forwarded-For (Render does), only the
    right-most hop is trustworthy: everything to its left was supplied by the client and can be spoofed. So when
    ``trust_proxy_headers`` is True we take the right-most hop (across repeated headers too) and, if it is
    missing or not a valid IP, fall back to the peer address. We never walk further left.
    """
    if trust_proxy_headers:
        values = request.headers.getlist("x-forwarded-for")
        if values:
            hop = _valid_ip(",".join(values).split(",")[-1])
            if hop:
                return hop
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
