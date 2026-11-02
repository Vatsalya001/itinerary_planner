"""Helpers shared by graph nodes: tracer binding, degradations, deadline constants and user-facing texts."""

from __future__ import annotations

from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from app.graph.state import RunContext
from app.trace import Tracer

# Every node routes to the deterministic planner when less than this many seconds remain.
DEADLINE_RESERVE_S = 12.0
MIN_LLM_CALL_S = 1.0

PARSER_DEGRADED = "AI parser unavailable; used rule-based parsing"
AGENT_TIMEOUT = "The AI planner ran out of time; switched to the rule-based planner"
AGENT_REFUSED = "The AI model declined this request; switched to the rule-based planner"
AGENT_UNAVAILABLE = "The AI planner was unavailable; switched to the rule-based planner"
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
