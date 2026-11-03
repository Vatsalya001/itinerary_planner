"""Graph state, reducers and the per-call run context.

Domain objects are stored as JSON-compatible dicts (``model_dump(mode="json")``) so every checkpointer
(in-memory, SQLite, Postgres) serializes them without custom types; nodes re-hydrate them with the
``get_*`` helpers. ``messages`` uses ``add_messages`` and is append-only: nodes only ever return new
messages, never edited or removed ones (Claude's thinking blocks must be passed back unchanged).

The run context (``RunContext``) carries per-call objects that must never be checkpointed: providers,
the tracer, the run ID and the deadline. A resumed thread gets a fresh context (new run ID and deadline).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from app.deps import Deps
from app.domain.schemas import (
    Candidate,
    ClarificationQuestion,
    Constraint,
    Location,
    ParseOutcome,
    PlanRequest,
    Preferences,
    WorldContext,
)
from app.llm.usage import add_usage
from app.trace import Tracer

Route = Literal[
    "parse_preferences", "resolve_location", "check_clarity", "ask_clarification", "build_context",
    "plan_agent", "tools", "validate_plan", "fallback_planner", "explain_infeasible", "finalize",
]


def merge_ledger(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    """First record seen for an ID wins (IDs are stable within a run)."""
    out = dict(a or {})
    for key, value in (b or {}).items():
        out.setdefault(key, value)
    return out


def add_unique(a: list[str] | None, b: list[str] | None) -> list[str]:
    out = list(a or [])
    for item in b or []:
        if item not in out:
            out.append(item)
    return out


class PlannerState(TypedDict, total=False):
    model_id: str  # requested model (registry ID)
    request: dict[str, Any]  # PlanRequest JSON (normalized; clarification answers folded in)
    extra_assumptions: Annotated[list[str], add_unique]
    injection_flagged: bool
    parse: dict[str, Any]  # ParseOutcome JSON
    matches: list[dict[str, Any]] | None  # geocoder matches (Location JSON); None = not looked up / failed
    lookup_failed: bool
    asked_before: bool
    questions: list[dict[str, Any]]  # ClarificationQuestion JSON
    prefs: dict[str, Any]  # final Preferences JSON (after apply_defaults)
    context: dict[str, Any]  # WorldContext JSON
    enforced: list[dict[str, Any]]  # merge(prefs.hard_constraints, original constraints)
    original: list[dict[str, Any]]  # constraints straight from the raw request
    messages: Annotated[list[AnyMessage], add_messages]
    ledger: Annotated[dict[str, dict[str, Any]], merge_ledger]  # every candidate any tool returned
    places_mode: str  # live | snapshot | unavailable
    events_failed: bool
    pending_submit: dict[str, Any] | None  # {"id": tool_call_id, "args": {...}}
    submit_attempts: int
    first_submit_valid: bool | None
    agent_turns: int
    turn_cap: int  # agent turns allowed this run (settings cap, tightened per model; set on the first turn)
    nudges: int
    protocol_errors: int
    protocol_error_kinds: dict[str, int]  # unknown_tool, malformed_args, schema_invalid, unknown_id, cap_reached, ...
    agent_stop: str | None  # why the agent loop ended without an accepted plan (degradation decided by fallback)
    checked_draft: dict[str, Any] | None  # PlanDraft JSON of the last check_plan call when it had no errors
    filled_minutes: int  # minutes app.domain.fill added to the accepted agent draft (no model call)
    tool_calls: dict[str, int]  # calls the model requested, by tool name
    tool_exec: dict[str, int]  # calls executed (caps apply to these)
    usage: Annotated[dict[str, Any], add_usage]
    model_used: str | None  # registry ID of the model that answered last
    degradations: Annotated[list[str], add_unique]
    route: Route  # set by each node; conditional edges read it
    final: dict[str, Any]  # inputs for finalize: status, mode, draft, validation, infeasible, message


@dataclass
class RunContext:
    deps: Deps
    tracer: Tracer
    run_id: str
    model_id: str
    started: float  # time.perf_counter() at the start of this call
    deadline: float  # absolute time.perf_counter() deadline for this call
    usage_base: dict[str, Any] | None = None  # usage already reported before this call (resume after a pause)

    def remaining(self) -> float:
        return self.deadline - time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


# ---------------------------------------------------------------------------------------------------
# Hydration helpers
# ---------------------------------------------------------------------------------------------------


def get_request(state: PlannerState) -> PlanRequest:
    return PlanRequest.model_validate(state.get("request") or {})


def get_parse(state: PlannerState) -> ParseOutcome:
    return ParseOutcome.model_validate(state["parse"])


def get_matches(state: PlannerState) -> list[Location] | None:
    raw = state.get("matches")
    return None if raw is None else [Location.model_validate(m) for m in raw]


def get_questions(state: PlannerState) -> list[ClarificationQuestion]:
    return [ClarificationQuestion.model_validate(q) for q in state.get("questions") or []]


def get_prefs(state: PlannerState) -> Preferences:
    return Preferences.model_validate(state["prefs"])


def get_ctx(state: PlannerState) -> WorldContext:
    return WorldContext.model_validate(state["context"])


def get_constraints(state: PlannerState, key: Literal["enforced", "original"]) -> list[Constraint]:
    return [Constraint.model_validate(c) for c in state.get(key) or []]


def get_ledger(state: PlannerState) -> dict[str, Candidate]:
    return {cid: Candidate.model_validate(raw) for cid, raw in (state.get("ledger") or {}).items()}


def dump(obj: Any) -> Any:
    return obj.model_dump(mode="json")
