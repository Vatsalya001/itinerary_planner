"""Shared data contracts for the Perfect Saturday Planner.

Every module (domain, providers, graph, API, evals) exchanges these models. Conventions:

* Local times are integer minutes since local midnight on the target date. Values above 1440 mean
  "after midnight" (25:30 -> 1530). Durations are integer minutes.
* Money is whole currency units (rupees for INR) per person, as ``int``. ``currency`` is ISO 4217.
* Candidate IDs are provider-prefixed strings: ``gp:`` Google Places, ``snap:`` curated OSM snapshot,
  ``fx:`` frozen-world fixture, ``ev:`` web event, ``evfx:`` fixture event.
* Unknown evidence is never treated as a yes.
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Enums and small value types
# ---------------------------------------------------------------------------


class Energy(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Evidence(StrEnum):
    CONFIRMED = "confirmed"  # positive evidence from a provider field, type or curated flag
    LIKELY = "likely"  # weak signal (e.g. name contains "veg"); never satisfies a hard constraint
    UNKNOWN = "unknown"
    NO = "no"  # contrary evidence


Effort = Literal["low", "medium", "high"]
Status = Literal["complete", "fallback", "needs_clarification", "no_feasible_plan", "rejected"]
Mode = Literal["agent", "deterministic"]
CostBasis = Literal["listed_price_range", "price_level_estimate", "ticket_price", "free", "category_estimate"]
ProviderName = Literal["google_places", "curated_snapshot", "fixture", "web_event", "fixture_event"]
TravelMode = Literal["walk", "auto", "cab", "metro"]

ConstraintKind = Literal["diet", "alcohol", "access", "crowd", "setting", "must_include", "must_avoid"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Request and preferences
# ---------------------------------------------------------------------------


class PlanRequest(StrictModel):
    """API input. Accepts the brief's JSON as-is, plus optional free text and extras."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    free_text: str | None = Field(default=None, max_length=1000)
    city: str | None = Field(default=None, max_length=80)
    budget: float | None = Field(default=None, ge=0, le=1_000_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    available_time: str | int | None = None  # "4 hours", "90 min", 240
    start_time: str | None = Field(default=None, max_length=20)  # "15:00", "3pm"
    date: Date | None = None
    mood: str | None = Field(default=None, max_length=300)
    interests: list[str] = Field(default_factory=list, max_length=10)
    constraints: list[str] = Field(default_factory=list, max_length=10)
    neighbourhood: str | None = Field(default=None, max_length=80)
    group_size: int = Field(default=1, ge=1, le=20)
    # Constraint keys the user chose to drop via a one-tap relaxation, e.g. ["access:wheelchair"].
    # Parsers must not re-derive these from free text or fields.
    relaxed_constraints: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("available_time")
    @classmethod
    def _bounded_time(cls, v: str | int | None) -> str | int | None:
        """Numeric minutes must be 30-720. Text is parsed later and clamped to the same range."""
        if v is None:
            return v
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            if len(str(v).strip()) > 4 or not 30 <= int(v) <= 720:
                raise ValueError("available_time must be between 30 and 720 minutes")
            return int(v)
        if len(v) > 60:
            raise ValueError("available_time text is too long")
        return v

    @field_validator("relaxed_constraints")
    @classmethod
    def _relaxed_keys(cls, v: list[str]) -> list[str]:
        from app.domain.constraints import VOCABULARY

        out: list[str] = []
        for key in v:
            kind, _, value = key.partition(":")
            if kind == "requirement" and re.fullmatch(r"[a-f0-9]{12}", value):
                if key not in out:
                    out.append(key)
                continue
            if value not in VOCABULARY.get(kind, set()):
                raise ValueError(f"unknown constraint key: {key[:40]}")
            if key not in out:
                out.append(key)
        return out

    @field_validator("interests")
    @classmethod
    def _short_interests(cls, v: list[str]) -> list[str]:
        if any(len(i) > 40 for i in v):
            raise ValueError("each interest must be at most 40 characters")
        return [i.strip() for i in v if i.strip()]

    @field_validator("constraints")
    @classmethod
    def _short_constraints(cls, v: list[str]) -> list[str]:
        if any(len(c) > 120 for c in v):
            raise ValueError("each constraint must be at most 120 characters")
        return [c.strip() for c in v if c.strip()]


class Constraint(StrictModel):
    """A hard constraint in the fixed vocabulary (see app.domain.constraints.VOCABULARY)."""

    kind: ConstraintKind
    value: str
    source_text: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"

    def __hash__(self) -> int:  # allow sets of constraints keyed by (kind, value)
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Constraint) and other.key == self.key


class ConstraintConflict(StrictModel):
    a: Constraint
    b: Constraint
    reason: str


class VenueRequirement(StrictModel):
    """A user-authored venue requirement; extraction alone is never evidence of satisfaction."""

    id: str = Field(max_length=80)
    description: str = Field(max_length=240)
    mandatory: bool = True
    scope: Literal["all_stops", "food_stops", "any_stop"] = "all_stops"


class VenueRequirementEvidence(StrictModel):
    """Source-backed assessment. Unknown never satisfies a mandatory requirement."""

    requirement_id: str = Field(max_length=80)
    description: str = Field(default="", max_length=240)
    status: Literal["confirmed", "contradicted", "unknown"] = "unknown"
    source_url: str | None = Field(default=None, max_length=2048)
    retrieved_at: datetime | None = None
    quote: str | None = Field(default=None, max_length=500)
    reason: str = Field(default="", max_length=500)


class Preferences(StrictModel):
    """Normalized preferences. ``city`` may be None only in a partial parse awaiting clarification."""

    city: str | None = None
    budget: int | None = None  # per person, whole currency units
    currency: str | None = None
    start_min: int = 900  # 15:00 default
    available_minutes: int = 240
    date: Date | None = None
    mood: str = ""
    energy: Energy = Energy.MEDIUM
    interests: list[str] = Field(default_factory=list)
    hard_constraints: list[Constraint] = Field(default_factory=list)
    venue_requirements: list[VenueRequirement] = Field(default_factory=list, max_length=5)
    soft_preferences: list[str] = Field(default_factory=list)
    neighbourhood: str | None = None
    group_size: int = 1
    assumptions: list[str] = Field(default_factory=list)  # visible defaults that were applied


class ParseOutcome(StrictModel):
    preferences: Preferences
    missing: list[str] = Field(default_factory=list)  # e.g. ["city"], ["budget", "available_time"]
    unmapped_constraints: list[str] = Field(default_factory=list)  # hard-sounding text not in vocabulary
    conflicts: list[ConstraintConflict] = Field(default_factory=list)
    budget_currency_explicit: bool = False  # True if the user named a currency or symbol
    source: Literal["llm", "rules"] = "rules"


class ClarificationQuestion(StrictModel):
    id: str  # "city" | "currency" | "conflict" | "budget_time" | "constraint" | "city_not_found"
    question: str
    options: list[str] = Field(default_factory=list)
    field: str | None = None


# ---------------------------------------------------------------------------
# World context
# ---------------------------------------------------------------------------


class Location(StrictModel):
    id: str  # e.g. "geonames:1277333" or "fixture:bangalore"
    name: str
    country_code: str  # ISO 3166-1 alpha-2
    admin1: str | None = None
    lat: float
    lng: float
    timezone: str  # IANA
    currency: str | None = None  # from data/countries.yaml
    population: int | None = None


class Weather(StrictModel):
    status: Literal["ok", "unavailable"]
    summary: str = ""
    precip_prob_max: int | None = None  # percent
    temp_max_c: float | None = None
    temp_min_c: float | None = None
    weather_code: int | None = None  # WMO code
    sunrise_min: int | None = None
    sunset_min: int | None = None

    @property
    def is_rainy(self) -> bool:
        return self.status == "ok" and (self.precip_prob_max or 0) >= 50


class PacingProfile(StrictModel):
    energy: Energy
    max_stops: int
    max_walk_minutes: int
    buffer_minutes: int
    max_long_legs: int  # legs longer than long_leg_minutes
    long_leg_minutes: int = 15
    min_seated_stops: int = 0


class MealWindow(StrictModel):
    name: Literal["breakfast", "lunch", "snack", "dinner"]
    start_min: int
    end_min: int


class WorldContext(StrictModel):
    location: Location
    date: Date
    start_min: int
    end_min: int
    weather: Weather
    pacing: PacingProfile
    meal_windows: list[MealWindow] = Field(default_factory=list)
    now: datetime


# ---------------------------------------------------------------------------
# Candidates (places and events)
# ---------------------------------------------------------------------------


class CostRange(StrictModel):
    min: int = Field(ge=0)
    max: int = Field(ge=0)
    currency: str
    basis: CostBasis


class Hours(StrictModel):
    """Opening hours on the target date. ``periods`` are [open_min, close_min] pairs.

    status="known" with no periods means closed all day. status="unknown" means no data.
    """

    status: Literal["known", "unknown"] = "unknown"
    periods: list[tuple[int, int]] = Field(default_factory=list)


class Source(StrictModel):
    provider: ProviderName
    url: str | None = None  # https only
    retrieved_at: datetime | None = None  # live fetch time
    last_checked: Date | None = None  # curated snapshot date
    attribution: str | None = None  # "Google Maps", "© OpenStreetMap contributors", site name


class Candidate(StrictModel):
    id: str
    kind: Literal["place", "event"] = "place"
    name: str
    category: str  # canonical category from data/categories.yaml
    city_id: str  # Location.id the candidate belongs to
    lat: float
    lng: float
    address: str | None = None
    neighbourhood: str | None = None
    cost: CostRange
    rating: float | None = None
    rating_count: int | None = None
    hours: Hours = Field(default_factory=Hours)
    diet: dict[str, Evidence] = Field(default_factory=dict)  # keys: vegetarian, vegan, jain
    wheelchair: Evidence = Evidence.UNKNOWN
    indoor: bool | None = None
    seated: bool = False
    effort: Effort = "medium"
    serves_alcohol: bool | None = None
    live_music: bool | None = None
    popularity: Literal["low", "medium", "high"] = "medium"
    busy_windows: list[tuple[int, int]] = Field(default_factory=list)  # likely-busy windows (heuristic)
    tags: list[str] = Field(default_factory=list)  # interest tags: food, coffee, music, walks, books, art...
    duration_range: tuple[int, int, int] = (30, 60, 120)  # min, default, max minutes
    event_start_min: int | None = None
    event_end_min: int | None = None
    event_date: Date | None = None
    maps_url: str | None = None
    source: Source
    website_url: str | None = Field(default=None, max_length=2048)
    requirement_evidence: list[VenueRequirementEvidence] = Field(default_factory=list, max_length=5)


class PlaceQuery(StrictModel):
    intent: str = Field(max_length=200)  # e.g. "quiet vegetarian cafe"
    category: str | None = None
    neighbourhood: str | None = Field(default=None, max_length=80)  # bias text/location to this area
    near_lat: float | None = None
    near_lng: float | None = None
    radius_m: int = Field(default=5000, ge=200, le=40000)
    max_price_level: int | None = Field(default=None, ge=0, le=4)
    open_at_min: int | None = None
    limit: int = Field(default=8, ge=1, le=20)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


class DraftStop(StrictModel):
    candidate_id: str
    duration_min: int = Field(ge=5, le=600)
    why: str = Field(max_length=300)
    tradeoff: str | None = Field(default=None, max_length=240)
    plan_b_candidate_id: str | None = None


class Omission(StrictModel):
    ref: str = Field(max_length=120)  # candidate ID or short idea
    reason: str = Field(max_length=240)
    name: str | None = Field(default=None, max_length=120)  # display name, filled server-side from the ledger


class PlanDraft(StrictModel):
    """What the agent (or the deterministic planner) proposes. Order of ``stops`` is visit order."""

    stops: list[DraftStop] = Field(min_length=1, max_length=8)
    omitted: list[Omission] = Field(default_factory=list, max_length=10)
    summary_note: str = Field(default="", max_length=400)


class Leg(StrictModel):
    from_id: str
    to_id: str
    mode: TravelMode
    distance_km: float
    minutes: int  # expected
    minutes_max: int  # conservative; used for scheduling
    fare: CostRange
    basis: Literal["estimate"] = "estimate"


class ScheduledStop(StrictModel):
    candidate: Candidate
    start_min: int
    end_min: int
    duration_min: int
    why: str
    tradeoff: str | None = None
    plan_b: Candidate | None = None
    warnings: list[str] = Field(default_factory=list)


class Totals(StrictModel):
    currency: str
    stops_cost_min: int
    stops_cost_max: int
    legs_cost_min: int
    legs_cost_max: int
    contingency: int  # ceil(10% of upper total)
    worst_case: int  # upper total + contingency
    budget: int | None
    budget_left: int | None  # budget - worst_case
    elapsed_min: int
    available_minutes: int
    minutes_left: int
    walking_min: int


class CanonicalPlan(StrictModel):
    date: Date
    city: str
    timezone: str
    start_min: int
    end_min: int
    stops: list[ScheduledStop]
    legs: list[Leg]
    totals: Totals
    summary_note: str = ""
    omitted: list[Omission] = Field(default_factory=list)
    weather: Weather
    maps_route_url: str | None = None


class Violation(StrictModel):
    code: str  # see app.domain.validator.CODES
    severity: Literal["error", "warning"]
    message: str
    stop_index: int | None = None
    candidate_id: str | None = None


class ValidationReport(StrictModel):
    ok: bool  # True when there are no error-severity violations
    violations: list[Violation] = Field(default_factory=list)
    plan: CanonicalPlan | None = None  # present whenever every stop could be hydrated and scheduled

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]


class CheckItem(StrictModel):
    name: str  # "budget", "time", "opening_hours", "diet", "accessibility", "crowds", "weather", "sources"
    status: Literal["pass", "warn", "unverified", "fail"]
    detail: str


class Relaxation(StrictModel):
    label: str  # "Raise budget to ₹850"
    patch: dict[str, Any]  # PlanRequest fields to change, e.g. {"budget": 850}


class InfeasibleReport(StrictModel):
    reason: str
    binding_constraints: list[str] = Field(default_factory=list)  # constraint keys or "budget"/"time"
    relaxations: list[Relaxation] = Field(default_factory=list)
    cheapest_budget: int | None = None
    min_minutes: int | None = None


class PlanResult(StrictModel):
    run_id: str
    status: Status
    mode: Mode | None = None
    model: str | None = None  # model that produced the plan, e.g. "ollama:qwen3.5:9b"
    plan: CanonicalPlan | None = None
    checks: list[CheckItem] = Field(default_factory=list)
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    thread_id: str | None = None  # resume handle when status == needs_clarification
    preferences: Preferences | None = None
    violations: list[Violation] = Field(default_factory=list)
    infeasible: InfeasibleReport | None = None
    degradations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    message: str | None = None  # user-facing message for rejected/no_feasible states
    share_id: str | None = None
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# Trace events (streamed over SSE)
# ---------------------------------------------------------------------------

TraceType = Literal[
    "run_started",
    "node_started",
    "node_completed",
    "tool_called",
    "tool_completed",
    "tool_failed",
    "validation",
    "degraded",
    "clarification",
    "result",
    "error",
    "heartbeat",
]


class TraceEvent(StrictModel):
    type: TraceType
    run_id: str
    seq: int
    ts: datetime
    node: str | None = None
    tool: str | None = None
    reason: str | None = None  # the agent's one-line reason for a tool call
    duration_ms: int | None = None
    summary: str | None = None
    data: dict[str, Any] | None = None  # for "result": PlanResult JSON; for "validation": violations
