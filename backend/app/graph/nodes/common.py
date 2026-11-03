"""Helpers shared by graph nodes: tracer binding, degradations, deadline constants and user-facing texts."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from app.domain.fill import fill_window, free_time_note
from app.domain.schemas import Candidate, CanonicalPlan, Constraint, PlanDraft, Preferences, WorldContext
from app.domain.validator import validate_plan
from app.graph.state import RunContext
from app.providers.base import TravelEstimator
from app.trace import Tracer

log = logging.getLogger("app.graph")

# Accepted agent drafts are stretched toward this share of the window (app.domain.fill.fill_window).
FILL_TARGET_RATIO = 0.85

# Every node routes to the deterministic planner when less than this many seconds remain.
DEADLINE_RESERVE_S = 12.0
MIN_LLM_CALL_S = 1.0

PARSER_DEGRADED = "AI parser unavailable; used rule-based parsing"
AGENT_TIMEOUT = "The AI planner ran out of time; switched to the rule-based planner"
AGENT_REFUSED = "The AI model declined this request; switched to the rule-based planner"
AGENT_UNAVAILABLE = "The AI planner was unavailable; switched to the rule-based planner"
AGENT_RATE_LIMITED = "The AI provider's rate limit was reached; switched to the rule-based planner"


def is_rate_limit_error(exc: BaseException) -> bool:
    """HTTP 429 / provider rate-limit errors (free tiers), told apart from outages for users and the eval report."""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__.lower()
    return status == 429 or "ratelimit" in name or "resourceexhausted" in name or "rate limit" in str(exc).lower()
AGENT_NOT_CONFIGURED = "The AI planner isn't configured; switched to the rule-based planner"
AGENT_NO_SUBMIT = "The AI planner didn't submit a plan; switched to the rule-based planner"
AGENT_TURN_CAP = "The AI planner ran out of turns; switched to the rule-based planner"
AGENT_NO_TIME = "Not enough time left for the AI planner; switched to the rule-based planner"
AGENT_REPAIRS_EXHAUSTED = "The AI planner's plan failed validation {n} times; switched to the rule-based planner"
AGENT_USED_CHECKED_DRAFT = "The AI planner ran out of time before submitting; used the plan it had already checked"
AGENT_CHECKED_NOT_SUBMITTED = "The AI planner didn't submit its plan; used the plan it had already checked"
# Stops after which a draft that already passed check_plan is re-validated and used (mode stays "agent"),
# mapped to the note shown with the plan.
SALVAGE_NOTES = {
    AGENT_TIMEOUT: AGENT_USED_CHECKED_DRAFT,
    AGENT_NO_TIME: AGENT_USED_CHECKED_DRAFT,
    AGENT_TURN_CAP: AGENT_USED_CHECKED_DRAFT,
    AGENT_NO_SUBMIT: AGENT_CHECKED_NOT_SUBMITTED,
}
WEATHER_ASSUMPTION = "Weather not checked"
OUT_OF_SCOPE_ASSUMPTION = "I can't book, pay or contact anyone — here's a plan you can act on"
TIME_CAPPED_ASSUMPTION = "Capped at 12 hours"
TIME_RAISED_ASSUMPTION = "Planning for at least 30 minutes"
MIN_AVAILABLE_MINUTES = 30
MAX_AVAILABLE_MINUTES = 720


def tracer_of(runtime: Runtime[RunContext]) -> Tracer:
    """The run's tracer, bound to this node's LangGraph stream writer (custom stream mode)."""
    tracer = runtime.context.tracer
    tracer.bind(get_stream_writer())
    return tracer


def degrade(tracer: Tracer, node: str, message: str, **data: object) -> list[str]:
    """Emit a "degraded" trace event and return the degradation list for the state update."""
    tracer.emit("degraded", node=node, summary=message, data=dict(data) if data else None)
    return [message]


def fill_draft(draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext, prefs: Preferences,
               enforced: list[Constraint], original: list[Constraint],
               travel: TravelEstimator) -> tuple[PlanDraft, int]:
    """Stretch an accepted agent draft toward the user's window with ``app.domain.fill.fill_window`` (no model
    call). The filled draft is re-validated and used only if it still has no errors; otherwise the original is
    kept. Returns (draft to use, minutes added)."""
    try:
        filled = fill_window(draft, ledger, ctx, prefs, enforced, travel, target_ratio=FILL_TARGET_RATIO)
    except Exception as exc:  # a fill bug must never cost the user a valid plan
        log.warning("fill_window failed", extra={"error_type": type(exc).__name__})
        return draft, 0
    added = sum(s.duration_min for s in filled.stops) - sum(s.duration_min for s in draft.stops)
    if filled is draft or added <= 0:
        return draft, 0
    if not validate_plan(filled, ledger, ctx, prefs, original, travel).ok:
        return draft, 0
    return filled, added


def free_time_warning(plan: CanonicalPlan, ctx: WorldContext) -> str | None:
    """Code-generated note about unused time (``app.domain.fill.free_time_note``), or None."""
    try:
        return free_time_note(plan, ctx)
    except Exception as exc:
        log.warning("free_time_note failed", extra={"error_type": type(exc).__name__})
        return None
