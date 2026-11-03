"""Safe provider error metadata for traces/logs, without response bodies or credentials."""
from __future__ import annotations

import math
from typing import Any


def provider_error_details(exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    details: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(status, int):
        details["http_status"] = status
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw = headers.get("retry-after")
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None and math.isfinite(seconds) and seconds >= 0:
            details["retry_after_s"] = seconds
    return details
