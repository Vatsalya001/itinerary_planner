"""Structured logging, Sentry and LangSmith gating.

* Logs are one JSON object per line with ``request_id`` and ``run_id`` from context variables. Callers pass only
  IDs, counts, durations and codes via ``extra=``; raw user text (free text, mood, constraints) is never logged.
* Sentry is initialised only when ``SENTRY_DSN`` is set, with ``send_default_pii=False``, no stack locals,
  release = git SHA, and a ``before_send`` hook that drops request bodies, cookies and query strings.
* LangSmith tracing is env-gated by ``TRACING_ENABLED``. We set the env vars LangSmith reads
  (``langsmith.utils.get_env_var`` checks ``LANGSMITH_<NAME>`` then ``LANGCHAIN_<NAME>``):
  ``LANGSMITH_TRACING`` / ``LANGSMITH_TRACING_V2`` ("true"/"false"; ``TRACING_V2`` is checked first, so both are
  set), ``LANGSMITH_PROJECT`` (default ``saturday-planner-<env>``), and in prod ``LANGSMITH_HIDE_INPUTS`` and
  ``LANGSMITH_HIDE_OUTPUTS`` = "true" (read by ``langsmith.Client``). Inputs are hidden as well as outputs because
  tool results (Google Places content) are fed back to the model as inputs of the next LLM call. Outside prod
  (evals on the frozen world) traces are kept in full.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)

_STD_ATTRS = set(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None))) | {"message", "asctime"}
_MAX_EXTRA = 400


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        run_id = getattr(record, "run_id", None) or run_id_var.get()
        if request_id:
            out["request_id"] = request_id
        if run_id:
            out["run_id"] = run_id
        for key, value in vars(record).items():
            if key in _STD_ATTRS or key in out or key.startswith("_"):
                continue
            if isinstance(value, str) and len(value) > _MAX_EXTRA:
                value = value[:_MAX_EXTRA] + "…"
            out[key] = value
        if record.exc_info:
            # Type and message only on stdout; full tracebacks go to Sentry (when configured).
            out["error"] = _describe(record.exc_info[1])
        return json.dumps(out, default=str, ensure_ascii=False)


def _describe(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):  # its str() echoes input values, which may be user text
        return f"ValidationError: {exc.error_count()} error(s) in {exc.title}"
    return f"{type(exc).__name__}: {str(exc)[:_MAX_EXTRA]}"


_HANDLER_NAME = "app-json"


def configure_logging(settings: Settings) -> None:
    """Idempotent: installs one JSON stdout handler on the root logger."""
    root = logging.getLogger()
    if any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "cookies", "query_string"):
            request.pop(key, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in {"authorization", "cookie", "x-forwarded-for"}:
                    headers.pop(key)
    return event


_sentry_ready = False


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry when a DSN is configured. Returns True if Sentry is active."""
    global _sentry_ready
    if settings.sentry_dsn is None or not settings.sentry_dsn.get_secret_value():
        return False
    if _sentry_ready:
        return True
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        release=settings.git_sha,
        environment=settings.app_env,
        send_default_pii=False,
        include_local_variables=False,  # stack locals can hold free text
        max_request_body_size="never",
        traces_sample_rate=0.0,
        before_send=_scrub_event,  # type: ignore[arg-type]
    )
    _sentry_ready = True
    return True


def configure_tracing(settings: Settings, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Set LangSmith env vars from settings. Returns the values it set (for tests and the startup log)."""
    env: Any = os.environ if environ is None else environ
    applied: dict[str, str] = {}
    has_key = bool(env.get("LANGSMITH_API_KEY") or env.get("LANGCHAIN_API_KEY"))
    enabled = settings.tracing_enabled and has_key
    flag = "true" if enabled else "false"
    for name in ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2"):
        applied[name] = flag
    if enabled:
        if not (env.get("LANGSMITH_PROJECT") or env.get("LANGCHAIN_PROJECT")):
            applied["LANGSMITH_PROJECT"] = f"saturday-planner-{settings.app_env}"
        hide = "true" if settings.is_prod else "false"
        applied["LANGSMITH_HIDE_INPUTS"] = hide
        applied["LANGSMITH_HIDE_OUTPUTS"] = hide
    env.update(applied)
    if environ is None:
        try:  # LangSmith memoises env lookups; clear so the new values take effect
            from langsmith import utils as ls_utils

            for fn in (ls_utils.get_env_var, ls_utils.get_tracer_project):
                getattr(fn, "cache_clear", lambda: None)()
        except Exception:  # pragma: no cover
            pass
    return applied


def setup_observability(settings: Settings) -> dict[str, Any]:
    configure_logging(settings)
    sentry = init_sentry(settings)
    tracing = configure_tracing(settings)
    info = {"sentry": sentry, "tracing": tracing.get("LANGSMITH_TRACING") == "true",
            "hide_io": tracing.get("LANGSMITH_HIDE_OUTPUTS") == "true"}
    logging.getLogger("app").info("observability configured", extra=info)
    return info
