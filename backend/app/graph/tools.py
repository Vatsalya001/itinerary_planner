"""Agent tools: JSON schemas shown to the model, strict argument models, and the executors.

Tools are read-only. Every returned candidate is added to the run's ledger by the tools node; the model
can only reference ledger IDs. Tool results are compact JSON (``ToolMessage.content``) with the full
Candidate records in ``ToolMessage.artifact["candidates"]``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.deps import Deps
from app.domain.categories import currency_symbol, is_category, load_categories
from app.domain.evidence import candidate_allowed, category_info, event_window, is_walking_stop, likely_busy
from app.domain.parser_rules import default_queries
from app.domain.schemas import (
    Candidate,
    CanonicalPlan,
    Constraint,
    CostRange,
    PlaceQuery,
    PlanDraft,
    Preferences,
    Violation,
    WorldContext,
)
from app.domain.travel import haversine_km
from app.domain.validator import draft_from_payload, duration_bounds, validate_plan
from app.graph.prompts import hhmm, parse_hhmm
from app.pipeline import EVENTS_UNAVAILABLE, PLACES_UNAVAILABLE, SNAPSHOT_USED
from app.providers.base import ProviderError

SEARCH_PLACES = "search_places"
SEARCH_EVENTS = "search_events"
ESTIMATE_TRAVEL = "estimate_travel"
CHECK_PLAN = "check_plan"
SUBMIT_PLAN = "submit_plan"
TOOL_NAMES = (SEARCH_PLACES, SEARCH_EVENTS, ESTIMATE_TRAVEL, CHECK_PLAN, SUBMIT_PLAN)

# Per-run caps (submit_plan's cap is settings.max_submit_attempts).
TOOL_CAPS: dict[str, int] = {SEARCH_PLACES: 3, SEARCH_EVENTS: 2, ESTIMATE_TRAVEL: 4, CHECK_PLAN: 4}
MAX_QUERIES_PER_CALL = 4  # search_places batches several queries into one call (one model request)
MAX_QUERIES_PER_RUN = 8
QUERIES_KEY = "search_places_queries"  # tool_exec key that counts executed queries

MAX_RESULTS = 8  # per query when a call has one query
BATCH_RESULTS = 6  # per query when a call has several (keeps the tool result, and every later prompt, small)
REASON_MAX = 140
NAME_MAX = 120
NEAR_RADIUS_M = 2500
NEIGHBOURHOOD_RADIUS_M = 3000
CITY_RADIUS_M = 8000

# ---------------------------------------------------------------------------------------------------
# JSON schemas shown to the model (OpenAI function format; every provider adapter accepts it)
# ---------------------------------------------------------------------------------------------------

_REASON = {"type": "string", "description": "Why you are making this call, under 140 characters. Shown to the user."}


def _stop_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string", "description": "An ID returned by search_places or search_events"},
            "duration_min": {"type": "integer", "description": "Minutes at this stop, within its duration range"},
            "why": {"type": "string", "description": "Why it fits this person. No prices, durations or times."},
            "tradeoff": {"type": "string", "description": "Optional: what was compromised. No numbers."},
            "plan_b_candidate_id": {"type": "string",
                                    "description": "Optional indoor alternative (a returned ID) if it rains"},
        },
        "required": ["candidate_id", "duration_min", "why"],
    }


def _plan_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "stops": {"type": "array", "items": _stop_schema(), "minItems": 1, "maxItems": 8,
                      "description": "Stops in visit order"},
            "omitted": {
                "type": "array", "maxItems": 10,
                "items": {"type": "object", "properties": {
                    "ref": {"type": "string", "description": "Candidate ID or a short idea that was left out"},
                    "reason": {"type": "string", "description": "Why it was left out. No numbers."},
                }, "required": ["ref", "reason"]},
            },
            "summary_note": {"type": "string", "description": "One or two sentences on the day. No numbers."},
            "reason": _REASON,
        },
        "required": ["stops", "summary_note"],
    }


def tool_schemas() -> list[dict[str, Any]]:
    """The five tools in OpenAI function format (bind_tools converts them per provider)."""
    categories = sorted(load_categories())
    return [
        {"type": "function", "function": {
            "name": SEARCH_PLACES,
            "description": (
                f"Search real places in the plan's city. Send 1-{MAX_QUERIES_PER_CALL} queries in ONE call (for "
                "example a meal, a cafe and a walk together). Returns candidates grouped per query with ID, name, "
                "category, distance, price with its basis, rating, hours on the target day, diet and wheelchair "
                "evidence, indoor/seated/effort, likely-busy windows and the allowed visit length. "
                f"At most {TOOL_CAPS[SEARCH_PLACES]} calls and {MAX_QUERIES_PER_RUN} queries per run."),
            "parameters": {"type": "object", "properties": {
                "queries": {
                    "type": "array", "minItems": 1, "maxItems": MAX_QUERIES_PER_CALL,
                    "description": ('The searches to run, all in this one call, e.g. [{"intent": "quiet vegetarian '
                                    'cafe", "category": "cafe"}, {"intent": "park for a walk", "category": "park"}]'),
                    "items": {"type": "object", "properties": {
                        "intent": {"type": "string",
                                   "description": "What to look for, e.g. 'quiet vegetarian cafe'"},
                        "category": {"type": "string", "enum": categories,
                                     "description": "Optional canonical category"},
                        "near_candidate_id": {"type": "string", "description":
                                              "Optional: search around this previously returned candidate"},
                        "max_price_level": {"type": "integer", "minimum": 0, "maximum": 4,
                                            "description": "Optional: 0 free .. 4 very expensive"},
                        "open_at": {"type": "string",
                                    "description": "Optional local time HH:MM the place must be open"},
                    }, "required": ["intent"]},
                },
                "include_events": {"type": "boolean", "description": (
                    "Also return events on the plan's date matching the interests, in this same call "
                    "(saves a separate search_events call)")},
                "reason": _REASON,
            }, "required": ["queries", "reason"]},
        }},
        {"type": "function", "function": {
            "name": SEARCH_EVENTS,
            "description": (
                "Find events on the plan's date in the city (title, start and end time, venue area, listed "
                f"price). At most {TOOL_CAPS[SEARCH_EVENTS]} calls per run."),
            "parameters": {"type": "object", "properties": {
                "interests": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                "max_price": {"type": "integer", "minimum": 0, "description": "Optional per-person ticket cap"},
                "reason": _REASON,
            }, "required": ["interests", "reason"]},
        }},
        {"type": "function", "function": {
            "name": ESTIMATE_TRAVEL,
            "description": (
                "Estimate travel legs (mode, minutes, fare range) between consecutive candidate IDs in the "
                f"given order. At most {TOOL_CAPS[ESTIMATE_TRAVEL]} calls per run."),
            "parameters": {"type": "object", "properties": {
                "candidate_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 8},
                "reason": _REASON,
            }, "required": ["candidate_ids", "reason"]},
        }},
        {"type": "function", "function": {
            "name": CHECK_PLAN,
            "description": (
                "Dry-run validation of a draft plan: returns the computed schedule, totals and every violation, "
                f"with no side effects. At most {TOOL_CAPS[CHECK_PLAN]} calls per run."),
            "parameters": _plan_schema("A draft plan, same shape as submit_plan"),
        }},
        {"type": "function", "function": {
            "name": SUBMIT_PLAN,
            "description": (
                "Submit the final plan; code validates it and returns the violations with a one-line fix if it "
                "fails (limited resubmissions). Most common rejections: time at stops marked walking:true plus "
                "walking legs above max_walking_minutes_total; a plan_b_candidate_id that is one of the stops, "
                "not indoor, or given when rain is not likely; visits outside hours_today or arriving after an "
                "event starts; the window filled with visit time and no room for moves and buffers; upper prices "
                "plus fares plus 10% above the budget; numbers in why/tradeoff/summary_note."),
            "parameters": _plan_schema("The final plan"),
        }},
    ]


# ---------------------------------------------------------------------------------------------------
# Argument models (validation of what the model actually sent)
# ---------------------------------------------------------------------------------------------------

_NULLISH = {"", "null", "none", "n/a"}


def _nullish(v: Any) -> Any:
    if isinstance(v, str) and v.strip().lower() in _NULLISH:
        return None
    return v


def clip_reason(v: Any) -> str:
    text = " ".join(str(v or "").split())
    return text[:REASON_MAX]


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    reason: str = ""

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> str:
        return clip_reason(v)


QUERY_KEYS = ("intent", "category", "near_candidate_id", "max_price_level", "open_at")


class PlaceQueryArgs(BaseModel):
    """One search inside a search_places call."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    intent: str = Field(min_length=1, max_length=200)
    category: str | None = None
    near_candidate_id: str | None = Field(default=None, max_length=120)
    max_price_level: int | None = Field(default=None, ge=0, le=4)
    open_at: str | None = None

    @field_validator("category", "near_candidate_id", "open_at", "max_price_level", mode="before")
    @classmethod
    def _optional(cls, v: Any) -> Any:
        return _nullish(v)

    @field_validator("category")
    @classmethod
    def _category(cls, v: str | None) -> str | None:
        if v is None:
            return None
        key = v.strip().lower().replace(" ", "_")
        if not is_category(key):
            raise ValueError(f"unknown category '{v[:40]}'; use one of: {', '.join(sorted(load_categories()))}")
        return key

    @field_validator("open_at")
    @classmethod
    def _open_at(cls, v: str | None) -> str | None:
        if v is not None and parse_hhmm(v) is None:
            raise ValueError("open_at must be a local time like 16:30")
        return v


class SearchPlacesArgs(_Args):
    """``queries`` holds the raw query objects; each is validated on its own (one bad query doesn't sink the
    batch). The old single-query shape (``intent`` etc. at the top level) is still accepted for small models."""

    queries: list[Any] = Field(min_length=1)
    include_events: bool = False

    @field_validator("include_events", mode="before")
    @classmethod
    def _flag(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and v.strip().lower() in _NULLISH):
            return False
        return v

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        out = dict(data)
        queries = _maybe_json(out.get("queries"))
        if isinstance(queries, Mapping):
            queries = [queries]
        if not queries and out.get("intent"):
            queries = [{k: out[k] for k in QUERY_KEYS if k in out}]
        if isinstance(queries, str):
            queries = [queries]
        if isinstance(queries, list):  # a plain string is shorthand for {"intent": ...} (small models send these)
            queries = [{"intent": q} if isinstance(q, str) else q for q in queries]
        out["queries"] = queries if isinstance(queries, list) else []
        return out


class SearchEventsArgs(_Args):
    interests: list[str] = Field(default_factory=list, max_length=10)
    max_price: int | None = Field(default=None, ge=0)

    @field_validator("interests", mode="before")
    @classmethod
    def _interests(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [part.strip() for part in v.split(",")]
        if isinstance(v, list):
            return [str(i)[:40] for i in v if str(i).strip()]
        return v

    @field_validator("max_price", mode="before")
    @classmethod
    def _max_price(cls, v: Any) -> Any:
        return _nullish(v)


class EstimateTravelArgs(_Args):
    candidate_ids: list[str] = Field(min_length=2, max_length=8)


ARG_MODELS: dict[str, type[_Args]] = {
    SEARCH_PLACES: SearchPlacesArgs, SEARCH_EVENTS: SearchEventsArgs, ESTIMATE_TRAVEL: EstimateTravelArgs,
}

_STOP_KEYS = ("candidate_id", "duration_min", "why", "tradeoff", "plan_b_candidate_id")


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def normalize_plan_payload(args: Any) -> Any:
    """Map check_plan/submit_plan arguments onto PlanDraft fields. Unknown keys are dropped; values are
    not coerced beyond JSON-decoding stringified arrays, so malformed input still fails SCHEMA_INVALID."""
    if not isinstance(args, Mapping):
        return args
    for wrapper in ("plan", "draft"):  # {"plan": {"stops": [...]}} -> {"stops": [...]}
        inner = _maybe_json(args.get(wrapper))
        if "stops" not in args and isinstance(inner, Mapping) and "stops" in inner:
            args = {**inner, "reason": args.get("reason")}
    stops = _maybe_json(args.get("stops"))
    if isinstance(stops, list):
        stops = [_maybe_json(s) for s in stops]  # stop objects sent as JSON strings
        stops = [{k: _nullish(s[k]) for k in _STOP_KEYS if k in s} if isinstance(s, Mapping) else s for s in stops]
        stops = [{k: v for k, v in s.items() if v is not None} if isinstance(s, dict) else s for s in stops]
    omitted = _maybe_json(args.get("omitted")) or []
    if isinstance(omitted, list):
        normalized = []
        for o in omitted:
            if isinstance(o, Mapping):
                ref = o.get("ref") or o.get("candidate_id") or o.get("idea") or ""
                normalized.append({"ref": str(ref)[:120], "reason": str(o.get("reason") or "")[:240]})
            elif isinstance(o, str):
                normalized.append({"ref": o[:120], "reason": ""})
        omitted = [o for o in normalized if o["ref"]]
    payload: dict[str, Any] = {"stops": stops, "omitted": omitted}
    note = args.get("summary_note")
    payload["summary_note"] = "" if note is None else note
    return payload


def validation_errors_text(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:4]:
        loc = ".".join(str(p) for p in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------------------------------
# Compact JSON for the model
# ---------------------------------------------------------------------------------------------------

_BASIS = {"listed_price_range": "listed price range", "price_level_estimate": "price-level estimate",
          "ticket_price": "ticket price", "free": "free", "category_estimate": "category estimate"}


def price_text(cost: CostRange) -> str:
    if cost.max == 0:
        return "free"
    sym = currency_symbol(cost.currency).strip()
    amount = f"{sym}{cost.min}" if cost.min == cost.max else f"{sym}{cost.min}-{cost.max}"
    return f"{amount} per person ({_BASIS.get(cost.basis, cost.basis)})"


def fare_text(fare: CostRange) -> str:
    if fare.max == 0:
        return "free"
    sym = currency_symbol(fare.currency).strip()
    amount = f"{sym}{fare.min}" if fare.min == fare.max else f"{sym}{fare.min}-{fare.max}"
    return f"{amount} (estimate)"


def with_turns_left(content: str, turns_left: int) -> str:
    """Add ``turns_left`` to a JSON tool result so the model can budget its remaining turns."""
    try:
        data = json.loads(content)
    except ValueError:
        return content
    if not isinstance(data, dict):
        return content
    data["turns_left"] = max(0, turns_left)
    return dumps(data)


def hours_text(c: Candidate) -> str:
    if c.hours.status != "known":
        return "unknown"
    if not c.hours.periods:
        return "closed"
    return ", ".join(f"{hhmm(a)}-{hhmm(b)}" for a, b in c.hours.periods)


def compact_candidate(c: Candidate, centre: tuple[float, float] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": c.id, "name": c.name[:NAME_MAX], "category": c.category}
    if c.kind == "event":
        out["kind"] = "event"
    if c.neighbourhood:
        out["neighbourhood"] = c.neighbourhood[:60]
    if centre is not None:
        out["distance_km"] = round(haversine_km(centre[0], centre[1], c.lat, c.lng), 1)
    out["price"] = price_text(c.cost)
    if c.rating is not None:
        out["rating"] = c.rating
        if c.rating_count is not None:
            out["rating_count"] = c.rating_count
    if c.kind == "event":
        window = event_window(c)
        out["event_date"] = c.event_date.isoformat() if c.event_date else "unknown"
        out["event_time"] = f"{hhmm(window[0])}-{hhmm(window[1])}" if window else "unknown"
    else:
        out["hours_today"] = hours_text(c)
    if c.diet:
        out["diet"] = {k: v.value for k, v in c.diet.items()}
    out["wheelchair"] = c.wheelchair.value
    out["indoor"] = "unknown" if c.indoor is None else c.indoor
    out["seated"] = c.seated
    out["effort"] = c.effort
    if is_walking_stop(c):
        out["walking"] = True  # the whole visit counts toward the walking limit
    if c.serves_alcohol:
        out["serves_alcohol"] = True
    if c.popularity == "high" and c.busy_windows:
        out["likely_busy"] = [f"{hhmm(a)}-{hhmm(b)}" for a, b in c.busy_windows]
    lo, hi = duration_bounds(c)
    out["visit_minutes"] = {"min": lo, "typical": max(lo, min(hi, c.duration_range[1])), "max": hi}
    if c.tags:
        out["tags"] = [t[:30] for t in c.tags[:8]]
    out["source"] = c.source.provider
    return out


def violation_dicts(violations: list[Violation], *, include_ids: bool = True) -> list[dict[str, Any]]:
    """Violations as plain dicts. ``include_ids=False`` drops model-provided IDs that were not in the ledger
    (UNKNOWN_ID), so invented IDs never reach the trace."""
    out = []
    for v in violations:
        item: dict[str, Any] = {"code": v.code, "severity": v.severity, "message": v.message}
        if v.stop_index is not None:
            item["stop_index"] = v.stop_index
        if v.candidate_id and (include_ids or v.code != "UNKNOWN_ID"):
            item["candidate_id"] = v.candidate_id[:120]
        out.append(item)
    return out


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------------------------------

PlacesMode = Literal["live", "snapshot", "unavailable"]


@dataclass
class ToolEnv:
    deps: Deps
    ctx: WorldContext
    prefs: Preferences
    enforced: list[Constraint]
    original: list[Constraint]
    ledger: dict[str, Candidate]
    places_mode: PlacesMode = "live"
    events_failed: bool = False
    event_queries_used: int = 0
    queries_used: int = 0  # search_places queries executed this run (cap MAX_QUERIES_PER_RUN)


@dataclass
class ToolOutcome:
    content: str
    status: Literal["success", "error"] = "success"
    candidates: list[Candidate] = field(default_factory=list)
    summary: str = ""
    protocol_kind: str | None = None  # "schema_invalid" | "unknown_id" (see finalize.run_metrics)
    provider_failed: bool = False  # emit tool_failed (with recovered flag) instead of tool_completed
    recovered: bool = False
    degradations: list[str] = field(default_factory=list)
    data: dict[str, Any] | None = None
    checked_draft: dict[str, Any] | None = None  # check_plan only: the draft, when it had no errors

    @property
    def protocol_error(self) -> bool:
        return self.protocol_kind is not None

    @property
    def executed(self) -> bool:
        """False when the call was rejected before running (it doesn't use up the tool's cap)."""
        return not (self.protocol_kind is not None and self.status == "error")

    @property
    def artifact(self) -> dict[str, Any] | None:
        if not self.candidates:
            return None
        return {"candidates": [c.model_dump(mode="json") for c in self.candidates]}


def error_outcome(message: str, *, protocol: bool = False, code: str = "TOOL_ERROR") -> ToolOutcome:
    """Error result for the model. ``protocol=True`` counts it as a protocol error of kind ``code.lower()``."""
    return ToolOutcome(content=dumps({"error": message}), status="error", summary=message[:200],
                       protocol_kind=code.lower() if protocol else None, data={"code": code})


def search_bias(prefs: Preferences, ctx: WorldContext) -> tuple[float | None, float | None, int]:
    """(lat, lng, radius) for agent searches: the neighbourhood when one was named, else the whole city
    (lat/lng None, so a neighbourhood named in the intent can still re-centre the search)."""
    if prefs.neighbourhood:
        queries = default_queries(prefs, ctx)
        if queries and queries[0].radius_m <= NEIGHBOURHOOD_RADIUS_M:
            return queries[0].near_lat, queries[0].near_lng, queries[0].radius_m
    return None, None, CITY_RADIUS_M


def _open_at_min(text: str | None, ctx: WorldContext) -> int | None:
    value = parse_hhmm(text)
    if value is None:
        return None
    if value < 1440 and ctx.end_min > 1440 and value < ctx.start_min:
        value += 1440  # the window crosses midnight
    return value


async def _search_one(q: PlaceQueryArgs, env: ToolEnv, limit: int
                      ) -> tuple[list[Candidate], tuple[float, float] | None, str | None]:
    """Run one query: (candidates, distance centre, error for the model). Falls back from the live provider to
    the curated snapshot (sticky for the run); sets ``env.places_mode`` to "unavailable" when both fail."""
    ctx = env.ctx
    lat, lng, radius = search_bias(env.prefs, ctx)
    if q.near_candidate_id:
        anchor = env.ledger.get(q.near_candidate_id)
        if anchor is None:
            return [], None, (f"near_candidate_id '{q.near_candidate_id[:60]}' was not returned by a tool in this "
                              "run; use an ID from earlier results or omit it")
        lat, lng, radius = anchor.lat, anchor.lng, NEAR_RADIUS_M
    extra: dict[str, Any] = {}
    if env.prefs.neighbourhood and "neighbourhood" in PlaceQuery.model_fields and not q.near_candidate_id:
        extra["neighbourhood"] = env.prefs.neighbourhood[:80]  # providers that support it bias to the area
    query = PlaceQuery(intent=q.intent, category=q.category, near_lat=lat, near_lng=lng, radius_m=radius,
                       max_price_level=q.max_price_level, open_at_min=_open_at_min(q.open_at, ctx), limit=limit,
                       **extra)
    found: list[Candidate] = []
    while env.places_mode != "unavailable":
        provider = env.deps.places if env.places_mode == "live" else env.deps.snapshot
        try:
            assert provider is not None
            found = await provider.search(query, ctx)
            break
        except ProviderError:
            snapshot = env.deps.snapshot
            if env.places_mode == "live" and snapshot is not None and snapshot.covers(ctx.location):
                env.places_mode = "snapshot"
            else:
                env.places_mode = "unavailable"
    found = [c for c in found if c.kind == "place" or c.event_date == ctx.date][:limit]
    centre = (lat, lng) if lat is not None and lng is not None else (ctx.location.lat, ctx.location.lng)
    return found, centre, None


async def run_search_places(args: SearchPlacesArgs, env: ToolEnv) -> ToolOutcome:
    """One call, 1-4 queries, results grouped per query. Each candidate is listed once in full (later queries
    that also matched it name it under "also"). Over-budget queries are skipped and reported."""
    ctx = env.ctx
    started_mode = env.places_mode
    if started_mode == "unavailable":
        return ToolOutcome(content=dumps({"error": PLACES_UNAVAILABLE}), status="error",
                           summary=PLACES_UNAVAILABLE, provider_failed=True, data={"code": "PLACES_UNAVAILABLE"})
    budget = MAX_QUERIES_PER_RUN - env.queries_used
    if budget <= 0:
        return error_outcome(f"search_places can run at most {MAX_QUERIES_PER_RUN} queries per run. Work with the "
                             "candidates you already have.", protocol=True, code="CAP_REACHED")
    raw_queries = list(args.queries)
    protocol_kind: str | None = None
    skipped: list[str] = []
    if len(raw_queries) > MAX_QUERIES_PER_CALL:
        skipped.append(f"{len(raw_queries) - MAX_QUERIES_PER_CALL} query(ies) over the {MAX_QUERIES_PER_CALL}-per-call "
                       "limit were not run")
        protocol_kind = "schema_invalid"
        raw_queries = raw_queries[:MAX_QUERIES_PER_CALL]
    if len(raw_queries) > budget:
        skipped.append(f"{len(raw_queries) - budget} query(ies) over the {MAX_QUERIES_PER_RUN}-per-run limit were "
                       "not run")
        protocol_kind = protocol_kind or "cap_reached"
        raw_queries = raw_queries[:budget]
    limit = MAX_RESULTS if len(raw_queries) == 1 else BATCH_RESULTS

    results: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    listed: set[str] = set()
    ran = 0
    for raw in raw_queries:
        label = str(raw.get("intent", ""))[:60] if isinstance(raw, Mapping) else ""
        try:
            q = PlaceQueryArgs.model_validate(raw if isinstance(raw, Mapping) else {})
        except ValidationError as exc:
            results.append({"query": label, "error": f"invalid query: {validation_errors_text(exc)}"})
            protocol_kind = protocol_kind or "schema_invalid"
            continue
        env.queries_used += 1
        found, centre, error = await _search_one(q, env, limit)
        if error is not None:
            results.append({"query": q.intent[:60], "error": error})
            protocol_kind = protocol_kind or "unknown_id"
            continue
        if env.places_mode == "unavailable":
            break
        ran += 1
        fresh = [c for c in found if c.id not in listed]
        fresh_ids = {c.id for c in fresh}
        listed.update(fresh_ids)
        candidates += fresh
        entry: dict[str, Any] = {"query": q.intent[:60], "count": len(found),
                                 "candidates": [compact_candidate(c, centre) for c in fresh]}
        also = [c.id for c in found if c.id not in fresh_ids]
        if also:
            entry["also"] = also
        results.append(entry)

    degradations: list[str] = []
    recovered = started_mode == "live" and env.places_mode == "snapshot"
    if recovered:
        degradations.append(SNAPSHOT_USED)
    if env.places_mode == "unavailable" and not candidates:
        return ToolOutcome(content=dumps({"error": PLACES_UNAVAILABLE + ". Do not invent places."}),
                           status="error", summary=PLACES_UNAVAILABLE, provider_failed=True,
                           degradations=[PLACES_UNAVAILABLE], data={"code": "PLACES_UNAVAILABLE"})
    if ran == 0:
        return error_outcome('No query ran. Send queries like [{"intent": "quiet vegetarian cafe", "category": '
                             '"cafe"}], using candidate IDs from this run only. '
                             + " ".join(str(r.get("error", "")) for r in results)[:300],
                             protocol=True, code=(protocol_kind or "schema_invalid").upper())
    body: dict[str, Any] = {"results": results}
    if env.places_mode == "snapshot":
        body["note"] = "Live place data is unavailable; these come from a curated snapshot. Hours may be unknown."
    if env.places_mode == "unavailable":
        body["note"] = PLACES_UNAVAILABLE + "; later queries were not run."
        degradations.append(PLACES_UNAVAILABLE)
    if skipped:
        body["skipped"] = skipped
    provider_name = env.deps.snapshot.name if env.places_mode == "snapshot" and env.deps.snapshot else \
        getattr(env.deps.places, "name", "places")
    bits = [f"{ran} quer{'y' if ran == 1 else 'ies'}, {len(candidates)} places ({provider_name})"]
    diet = next((c.value for c in env.enforced if c.kind == "diet"), None)
    if diet:
        confirmed = sum(1 for c in candidates if c.diet.get(diet) is not None and c.diet[diet].value == "confirmed")
        bits.append(f"{confirmed} with {diet} evidence")
    busy = sum(1 for c in candidates if likely_busy(c, ctx.start_min, ctx.end_min))
    if busy:
        bits.append(f"{busy} likely busy")
    data: dict[str, Any] = {"count": len(candidates), "queries": ran}
    if args.include_events:
        events = await run_search_events(SearchEventsArgs(), env)
        event_body = json.loads(events.content)
        if events.status == "error":
            body["events_error"] = event_body.get("error")
            degradations += events.degradations
        else:
            body["events"] = event_body.get("events", [])
            body["events_date"] = event_body.get("date")
        candidates += events.candidates
        bits.append(events.summary)
        data["events"] = len(events.candidates)
    if recovered:
        data["recovered"] = True
    return ToolOutcome(content=dumps(body), candidates=candidates, summary=", ".join(bits), recovered=recovered,
                       degradations=degradations, data=data, protocol_kind=protocol_kind)


async def run_search_events(args: SearchEventsArgs, env: ToolEnv) -> ToolOutcome:
    ctx = env.ctx
    if env.events_failed:
        return error_outcome("Events unavailable; continue with the places already found", code="EVENTS_UNAVAILABLE")
    if env.event_queries_used >= TOOL_CAPS[SEARCH_EVENTS]:
        return error_outcome("Event search limit reached; use available candidates", code="CAP_REACHED")
    events_provider = env.deps.events
    if events_provider is None:
        return ToolOutcome(content=dumps({"count": 0, "events": [], "note": "Event search is not enabled"}),
                           summary="event search not enabled", data={"count": 0})
    env.event_queries_used += 1
    interests = args.interests or env.prefs.interests
    max_price = args.max_price if args.max_price is not None else env.prefs.budget
    try:
        found = await events_provider.search(interests, ctx, max_price=max_price)
    except ProviderError:
        env.events_failed = True
        return ToolOutcome(content=dumps({"error": EVENTS_UNAVAILABLE + "; plan without events"}), status="error",
                           summary=EVENTS_UNAVAILABLE, provider_failed=True, recovered=True,
                           degradations=[EVENTS_UNAVAILABLE], data={"code": "EVENTS_UNAVAILABLE"})
    found = [c for c in found if c.event_date == ctx.date][:MAX_RESULTS]
    centre = (ctx.location.lat, ctx.location.lng)
    body = {"count": len(found), "date": ctx.date.isoformat(),
            "events": [compact_candidate(c, centre) for c in found]}
    return ToolOutcome(content=dumps(body), candidates=found,
                       summary=f"{len(found)} event(s) on {ctx.date.isoformat()}", data={"count": len(found)})


def run_estimate_travel(args: EstimateTravelArgs, env: ToolEnv) -> ToolOutcome:
    missing = [cid for cid in args.candidate_ids if cid not in env.ledger]
    if missing:
        return error_outcome(f"unknown candidate ID(s): {', '.join(m[:60] for m in missing[:3])}; use IDs returned "
                             "by search_places or search_events", protocol=True, code="UNKNOWN_ID")
    cands = [env.ledger[cid] for cid in args.candidate_ids]
    legs = []
    total = 0
    for a, b in pairwise(cands):
        try:
            leg = env.deps.travel.leg(a, b, env.ctx)
        except ValueError:
            return error_outcome(f"travel from {a.id} to {b.id} could not be estimated", code="TRANSFER_UNKNOWN")
        total += leg.minutes_max
        legs.append({"from": a.id, "to": b.id, "mode": leg.mode, "distance_km": round(leg.distance_km, 1),
                     "minutes": leg.minutes, "minutes_max": leg.minutes_max, "fare": fare_text(leg.fare)})
    body = {"legs": legs, "total_minutes_max": total,
            "note": f"Estimates. The schedule adds a {env.ctx.pacing.buffer_minutes} minute buffer per transfer."}
    return ToolOutcome(content=dumps(body), summary=f"{len(legs)} leg(s), {total} min worst case",
                       data={"legs": len(legs)})


SUBMIT_NOW = "Now call submit_plan with exactly these stops."

# validator code -> the fix, phrased as an instruction (one line in tool results for small models)
_FIXES: dict[str, str] = {
    "UNKNOWN_ID": "use only IDs returned by search_places or search_events",
    "DUPLICATE_STOP": "remove the repeated stop",
    "DURATION_OUT_OF_RANGE": "set duration_min inside that stop's visit_minutes range",
    "BUDGET_EXCEEDED": "swap in cheaper or free stops, or drop one",
    "TIME_EXCEEDED": "shorten stops or drop one",
    "HOURS_CONFLICT": "pick a place open then, or reorder the stops",
    "DIET_UNVERIFIED": "use food stops with confirmed diet evidence",
    "DIET_CONFLICT": "use food stops with confirmed diet evidence",
    "ACCESSIBILITY_UNVERIFIED": "use stops with confirmed wheelchair access",
    "ALCOHOL_CONFLICT": "replace the alcohol venue",
    "SETTING_CONFLICT": "replace the stop that breaks the indoor/outdoor rule",
    "MUST_INCLUDE_MISSING": "add a stop that provides the required item",
    "MUST_AVOID_VIOLATED": "replace the stop the person wants to avoid",
    "CROWD_LIKELY": "avoid stops that are likely busy at that time",
    "PACING_EXCEEDED": "use fewer stops, less walking or shorter legs",
    "DAYLIGHT_CONFLICT": "move the outdoor walk before sunset",
    "EVENT_DATE_MISMATCH": "use only events on the plan's date",
    "SOURCE_MISSING": "use a different candidate",
    "CITY_MISMATCH": "use places in the plan's city",
    "TRANSFER_UNKNOWN": "use a different candidate",
    "PLAN_B_INVALID": "use an indoor returned candidate as plan_b_candidate_id, or leave it out",
    "PROSE_NUMERIC_CLAIM": "remove prices, durations, clock times and distances from the text",
    "SCHEMA_INVALID": ('send "stops" as a list of objects like {"candidate_id": "<id from results>", '
                       '"duration_min": 60, "why": "<one sentence>"}'),
}


# Message helper only (the validator decides): spots the number-like words to quote back to the model.
_NUMBERISH = re.compile(
    r"(?:₹|\$|£|€|rs\.?\s*)?\d+(?:[.,:]\d+)*[\s-]*(?:a\.?m\.?|p\.?m\.?|hours?|hrs?|h\b|minutes?|mins?|km|kms|"
    r"kilomet(?:er|re)s?|met(?:er|re)s?|m\b|rupees?|inr|rs\b)?",
    re.IGNORECASE)


def _prose_fix(v: Violation, draft: PlanDraft | None) -> str:
    """Quote the offending words so a model can remove them (small models resubmit the same text otherwise)."""
    fields: list[tuple[str, str]] = []
    if draft is not None and v.stop_index is not None and v.stop_index < len(draft.stops):
        stop = draft.stops[v.stop_index]
        fields = [("why", stop.why), ("tradeoff", stop.tradeoff or "")]
    elif draft is not None:
        fields = [("summary_note", draft.summary_note)] + [("omitted reason", o.reason) for o in draft.omitted]
    for name, text in fields:
        found = [m.group(0).strip().rstrip(".,;:") for m in _NUMBERISH.finditer(text) if m.group(0).strip()]
        if found:
            quoted = ", ".join(f'"{f[:30]}"' for f in dict.fromkeys(found))
            return f"rewrite {name} without {quoted} (no prices, durations, clock times or distances)"
        if text and v.message.startswith(f"'{name}'"):
            return f'rewrite {name} without numbers or number words: "{text[:80]}"'
    return _FIXES["PROSE_NUMERIC_CLAIM"]


# Codes where swapping the stop for another returned candidate is the fix; the line then names some.
_SWAPPABLE = {"DIET_UNVERIFIED", "DIET_CONFLICT", "ACCESSIBILITY_UNVERIFIED", "ALCOHOL_CONFLICT", "SETTING_CONFLICT",
              "MUST_AVOID_VIOLATED"}


def _alternatives(v: Violation, draft: PlanDraft | None, ledger: Mapping[str, Candidate],
                  constraints: list[Constraint], ctx: WorldContext | None, limit: int = 2) -> list[str]:
    """Returned candidates of the same kind and category group that pass every time-independent hard
    constraint and aren't already stops (small models otherwise resubmit the same failing stop)."""
    if draft is None or ctx is None or v.stop_index is None or v.stop_index >= len(draft.stops):
        return []
    current = ledger.get(draft.stops[v.stop_index].candidate_id)
    if current is None:
        return []
    used = {s.candidate_id for s in draft.stops}
    info = category_info(current)
    out: list[str] = []
    for c in ledger.values():
        if c.id in used or c.kind != current.kind:
            continue
        other = category_info(c)
        if info is not None and (other is None or other.group != info.group):
            continue
        if candidate_allowed(c, constraints, ctx)[0]:
            out.append(c.id)
            if len(out) == limit:
                break
    return out


def _scheduled_fix(v: Violation, plan: CanonicalPlan | None, ctx: WorldContext | None) -> str | None:
    """Quantitative fixes from the computed schedule: how many minutes to cut, and when things are open or dark
    (small models fix "over by 1 minute" only when told the number)."""
    if plan is None:
        return None
    t = plan.totals
    stop = plan.stops[v.stop_index] if v.stop_index is not None and v.stop_index < len(plan.stops) else None
    if v.code == "TIME_EXCEEDED":
        over = t.elapsed_min - t.available_minutes
        if over > 0:
            return f"the plan runs {over} min over the window: cut visit time by at least {over} min, or drop a stop"
        return "the plan ends after the window ends: cut visit time or drop a stop"
    if v.code == "PACING_EXCEEDED" and "walking" in v.message and ctx is not None:
        over = t.walking_min - ctx.pacing.max_walk_minutes
        return (f"walking is {over} min over the limit: shorten or drop the walking:true stop, or choose stops "
                "closer together")
    if v.code == "PACING_EXCEEDED" and "legs" in v.message:
        return "too many long moves: put nearby stops next to each other, or swap one for a closer place"
    if v.code == "PACING_EXCEEDED" and "stops" in v.message:
        return "drop a stop"
    if stop is not None and v.code == "HOURS_CONFLICT" and stop.candidate.kind != "event":
        return (f"it is scheduled {hhmm(stop.start_min)}-{hhmm(stop.end_min)} but open {hours_text(stop.candidate)}: "
                "move it to a time it is open, shorten it, or swap it")
    if stop is not None and v.code == "DAYLIGHT_CONFLICT" and plan.weather.sunset_min is not None:
        return (f"it ends {hhmm(stop.end_min)}, after sunset at {hhmm(plan.weather.sunset_min)}: put it first, "
                "shorten it, or drop it")
    if stop is not None and v.code == "CROWD_LIKELY":
        return (f"it is likely busy around {hhmm(stop.start_min)}: visit it at another time or pick a quieter "
                "place")
    return None


def fix_line(errors: list[Violation], draft: PlanDraft | None = None, *,
             ledger: Mapping[str, Candidate] | None = None, constraints: list[Constraint] | None = None,
             ctx: WorldContext | None = None, plan: CanonicalPlan | None = None) -> str:
    """All fixes for a failed check or submission, in one line: "Fix: stop 2: ...; plan: ...". With the ledger,
    constraints and context, constraint failures name concrete replacement candidates; with the computed
    plan, time, walking, hours and daylight fixes say by how much."""
    parts: list[str] = []
    for v in errors:
        if v.code == "PROSE_NUMERIC_CLAIM":
            fix = _prose_fix(v, draft)
        elif v.code == "HOURS_CONFLICT" and "event" in v.message:
            fix = "arrive before the event starts: move it earlier, or shorten or drop the stop before it"
        else:
            fix = _scheduled_fix(v, plan, ctx) or _FIXES.get(v.code, v.message)
        if v.code in _SWAPPABLE and ledger is not None:
            alternatives = _alternatives(v, draft, ledger, constraints or [], ctx)
            if alternatives:
                fix += ", e.g. replace it with " + " or ".join(alternatives)
        where = f"stop {v.stop_index + 1}" if v.stop_index is not None else "plan"
        item = f"{where}: {fix} ({v.code})"
        if item not in parts:
            parts.append(item)
    return "Fix: " + "; ".join(parts) + "." if parts else ""


def schedule_view(plan: CanonicalPlan | None) -> dict[str, Any]:
    """The computed schedule, legs and totals (check_plan results and rejected submissions)."""
    if plan is None:
        return {}
    t = plan.totals
    return {
        "schedule": [{"id": s.candidate.id, "name": s.candidate.name[:NAME_MAX], "start": hhmm(s.start_min),
                      "end": hhmm(s.end_min), "duration_min": s.duration_min} for s in plan.stops],
        "legs": [{"from": leg.from_id, "to": leg.to_id, "mode": leg.mode, "minutes_max": leg.minutes_max}
                 for leg in plan.legs],
        "totals": {"currency": t.currency, "worst_case_cost": t.worst_case, "budget": t.budget,
                   "budget_left": t.budget_left, "elapsed_min": t.elapsed_min,
                   "available_minutes": t.available_minutes, "minutes_left": t.minutes_left,
                   "walking_min": t.walking_min},
    }


def run_check_plan(args: Any, env: ToolEnv) -> ToolOutcome:
    draft, schema_errors = draft_from_payload(normalize_plan_payload(args))
    if draft is None:
        message = schema_errors[0].message if schema_errors else "draft failed schema validation"
        return error_outcome(f"{message}. Expected stops: [{{candidate_id, duration_min, why, tradeoff?, "
                             "plan_b_candidate_id?}], omitted, summary_note", protocol=True, code="SCHEMA_INVALID")
    report = validate_plan(draft, env.ledger, env.ctx, env.prefs, env.original, env.deps.travel)
    # the next step comes first, so small models read it before the details
    body: dict[str, Any] = {"ok": report.ok,
                            "next_step": SUBMIT_NOW if report.ok else fix_line(
                                report.errors, draft, ledger=env.ledger, constraints=env.enforced, ctx=env.ctx,
                                plan=report.plan)}
    body.update(schedule_view(report.plan))
    body["errors"] = violation_dicts(report.errors)
    body["warnings"] = violation_dicts(report.warnings)
    codes = sorted({v.code for v in report.errors})
    summary = "draft valid" if report.ok else f"{len(report.errors)} error(s): {', '.join(codes)}"
    return ToolOutcome(content=dumps(body), summary=summary, data={"ok": report.ok, "codes": codes},
                       protocol_kind="unknown_id" if "UNKNOWN_ID" in codes else None,
                       checked_draft=draft.model_dump(mode="json") if report.ok else None)
