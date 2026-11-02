"""guard_input: Unicode normalization, prompt-injection flagging and out-of-scope detection.

Nothing is rejected here: the API already enforced body size and field bounds, injection-like text is only
flagged in the trace (the parser never lets it set a field and the agent sees it as JSON data), and
booking/payment/messaging requests get a fixed assumption while planning continues.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from langgraph.runtime import Runtime
from pydantic import ValidationError

from app.domain.schemas import PlanRequest
from app.domain.timeutil import parse_duration
from app.graph.nodes.common import (
    MAX_AVAILABLE_MINUTES,
    MIN_AVAILABLE_MINUTES,
    OUT_OF_SCOPE_ASSUMPTION,
    TIME_CAPPED_ASSUMPTION,
    TIME_RAISED_ASSUMPTION,
    tracer_of,
)
from app.graph.state import PlannerState, RunContext, dump, get_request

INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:(?:all|any|the|your|my|of|these|those|previous|prior"
    r"|above|earlier|system|safety)\s+)*(?:instructions?|prompts?|rules|guidelines|directions|guardrails)\b"
    r"|\b(?:system|developer|assistant|admin)\s*:"
    r"|\bsystem\s+prompt\b|\bapi[\s_-]*keys?\b"
    r"|\b(?:reveal|print|show|output|leak|repeat)\b.{0,30}\b(?:prompt|instructions|secrets?|keys?|passwords?"
    r"|tokens?)\b"
    r"|\bvalidation\b.{0,30}\b(?:disabled?|off|bypass\w*|skip\w*|passed|turned\s+off)\b"
    r"|\byou\s+are\s+now\b|\bjailbreak|\bdeveloper\s+mode\b|\bover[- ]budget\s+plan\b",
    re.IGNORECASE,
)

# Booking, paying or contacting someone. "books", "bookstore", "bookshop" never match: they are interests.
OUT_OF_SCOPE_RE = re.compile(
    r"\b(?:book|reserve)\s+(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+|my\s+|our\s+|some\s+|\d+\s+)?"
    r"(?:table|tickets?|seats?|cab|taxi|ride|uber|ola|room|spot|slot|reservation|entry|pass(?:es)?)\b"
    r"|\b(?:make|place)\s+(?:a\s+|the\s+|my\s+|our\s+)?(?:booking|reservation|payment)s?\b"
    r"|\b(?:can|could|will|would)\s+you\s+(?:please\s+)?(?:book|reserve|pay)\b"
    r"|\bplease\s+(?:book|reserve|pay)\b"
    r"|\b(?:pay|purchase|buy)\s+(?:for\s+)?(?:the\s+|my\s+|our\s+)?(?:tickets?|entry|bill|order)\b"
    r"|\b(?:send|text|message|email|e-mail|whatsapp|dm)\s+(?:a\s+message\s+to\s+)?(?:my\s+|the\s+|our\s+)?"
    r"(?:friends?|wife|husband|partner|girlfriend|boyfriend|family|mom|mum|dad|someone|everyone|them|venue"
    r"|restaurant|host)\b"
    r"|\b(?:call|ring|phone)\s+(?:the\s+)?(?:venue|restaurant|cafe|place|host)\b"
    r"|\border\s+(?:me\s+|us\s+)?(?:food|a\s+cab|an\s+uber|delivery|takeaway)\b",
    re.IGNORECASE,
)

_LIMITS = {"free_text": 1000, "city": 80, "mood": 300, "neighbourhood": 80, "start_time": 20}
_CONTROL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def normalize_text(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    clean = _CONTROL.sub("", unicodedata.normalize("NFKC", text)).strip()
    return clean[:limit]


def clamp_available_time(value: str | int | None) -> tuple[str | int | None, str | None]:
    """Bound the available time to 30-720 minutes. Returns (value, assumption) where the assumption is set
    when the value had to be changed ("20 hours" -> 720 with "Capped at 12 hours")."""
    if value is None:
        return None, None
    minutes = parse_duration(value)
    if minutes is None:
        return value, None
    if minutes > MAX_AVAILABLE_MINUTES:
        return MAX_AVAILABLE_MINUTES, TIME_CAPPED_ASSUMPTION
    if minutes < MIN_AVAILABLE_MINUTES:
        return MIN_AVAILABLE_MINUTES, TIME_RAISED_ASSUMPTION
    return value, None


def normalize_request(request: PlanRequest) -> tuple[PlanRequest, list[str]]:
    """NFKC-normalized, control-stripped request with the available time bounded; plus any assumptions."""
    data: dict[str, Any] = request.model_dump()
    for key, limit in _LIMITS.items():
        if isinstance(data.get(key), str):
            data[key] = normalize_text(data[key], limit)
    if isinstance(data.get("available_time"), str):
        data["available_time"] = normalize_text(data["available_time"], 40)
    data["available_time"], assumption = clamp_available_time(data.get("available_time"))
    data["interests"] = [t for t in (normalize_text(i, 40) for i in request.interests) if t]
    data["constraints"] = [t for t in (normalize_text(c, 120) for c in request.constraints) if t]
    try:
        clean = PlanRequest.model_validate(data)
    except ValidationError:  # normalization produced something the contract rejects: drop the time, keep the rest
        data["available_time"] = None
        clean = PlanRequest.model_validate(data)
    return clean, [assumption] if assumption else []


def request_texts(request: PlanRequest) -> list[str]:
    texts = [request.free_text, request.mood, request.city, request.neighbourhood, *request.constraints,
             *request.interests]
    return [t for t in texts if t]


def screen(request: PlanRequest) -> tuple[bool, bool]:
    """(injection_flagged, out_of_scope)."""
    texts = request_texts(request)
    return (any(INJECTION_RE.search(t) for t in texts), any(OUT_OF_SCOPE_RE.search(t) for t in texts))


async def guard_input(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    tracer = tracer_of(runtime)
    with tracer.node("guard_input") as info:
        request, assumptions = normalize_request(get_request(state))
        flagged, out_of_scope = screen(request)
        notes = list(assumptions)
        if flagged:
            notes.append("instruction-like text flagged and treated as data")
        if out_of_scope:
            notes.append("booking/payment/messaging request noted (not supported)")
        info["summary"] = "; ".join(notes) or "input normalized"
        info["data"] = {"injection_flagged": flagged, "out_of_scope": out_of_scope}
    if out_of_scope:
        assumptions.append(OUT_OF_SCOPE_ASSUMPTION)
    return {"request": dump(request), "injection_flagged": flagged, "extra_assumptions": assumptions,
            "route": "parse_preferences"}
