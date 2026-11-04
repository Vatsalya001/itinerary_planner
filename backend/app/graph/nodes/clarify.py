"""resolve_location, check_clarity and ask_clarification (LangGraph ``interrupt``).

Clarification happens before any place search, so a paused thread holds only user input and geocoder
matches. At most one round: after the answers are applied, ``asked_before`` is set and the policy
applies visible defaults instead of asking again.
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, interrupt

from app.domain.parser_rules import apply_answers, clarification_questions
from app.domain.schemas import PlanRequest
from app.graph.nodes.common import tracer_of
from app.graph.nodes.guard import clamp_available_time, normalize_request
from app.graph.state import (
    PlannerState,
    RunContext,
    dump,
    get_matches,
    get_parse,
    get_request,
)
from app.pipeline import geocode
from app.providers.base import ProviderError

GEOCODER_ATTEMPTS = 2


def _transient(exc: Exception) -> bool:
    return isinstance(exc, ProviderError) and exc.retryable


# Retry only transient geocoder failures (timeouts, 429, 5xx); the last attempt degrades instead of raising.
GEOCODER_RETRY = RetryPolicy(max_attempts=GEOCODER_ATTEMPTS, initial_interval=0.2, backoff_factor=2.0,
                             max_interval=1.0, jitter=True, retry_on=_transient)

CITY_NEEDED = "I still need to know which city you'll be in to plan your Saturday."
LOOKUP_FAILED = "I couldn't look up that city right now. Please try again shortly."


def city_not_found(city: str) -> str:
    return f"I couldn't find a city called “{city[:80]}”. Try the name of the nearest larger city."


async def resolve_location(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    city = get_parse(state).preferences.city
    attempt = runtime.execution_info.node_attempt if runtime.execution_info else GEOCODER_ATTEMPTS
    matches = None
    failed = False
    with tracer.node("resolve_location") as info:
        if city:
            try:
                matches = await geocode(city, rc.deps, tracer)
            except ProviderError as exc:
                if exc.retryable and attempt < GEOCODER_ATTEMPTS:
                    info["summary"] = "geocoder failed; retrying"
                    raise
                failed = True
        if not city:
            info["summary"] = "no city given"
        elif failed:
            info["summary"] = "geocoder unavailable"
        else:
            info["summary"] = f"{len(matches or [])} match(es)" + (f": {matches[0].name}" if matches else "")
    return {"matches": None if matches is None else [dump(m) for m in matches], "lookup_failed": failed,
            "route": "check_clarity"}


async def check_clarity(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    tracer = tracer_of(runtime)
    outcome = get_parse(state)
    matches = get_matches(state)
    asked = bool(state.get("asked_before"))
    with tracer.node("check_clarity") as info:
        questions = clarification_questions(outcome, matches, asked) if not state.get("lookup_failed") else []
        info["summary"] = f"{len(questions)} blocking question(s)" + (" (already asked once)" if asked else "")
    if questions:
        payload = [q.model_dump() for q in questions]
        tracer.emit("clarification", node="check_clarity", summary=questions[0].question,
                    data={"questions": payload})
        return {"questions": payload, "route": "ask_clarification"}
    if matches:
        return {"questions": [], "route": "build_context"}
    city = outcome.preferences.city
    if not city:
        message = CITY_NEEDED
    elif state.get("lookup_failed"):
        message = LOOKUP_FAILED
    else:
        message = city_not_found(city)
    return {"questions": [], "route": "finalize",
            "final": {"status": "no_feasible_plan", "mode": None, "message": message}}


async def ask_clarification(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    # interrupt() first: on resume this node re-runs from the top, so nothing is emitted before it.
    answers = interrupt({"questions": state.get("questions") or []})
    tracer = tracer_of(runtime)
    with tracer.node("ask_clarification") as info:
        clean: dict[str, str] = {}
        if isinstance(answers, dict):
            clean = {str(k)[:40]: str(v)[:200] for k, v in answers.items() if v is not None and str(v).strip()}
        normalized_answers = {k: normalize_request(PlanRequest(mood=v))[0].mood or "" for k, v in clean.items()}
        request, assumptions = normalize_request(apply_answers(get_request(state), normalized_answers))
        for key, value in normalized_answers.items():
            if key in {"available_time", "time", "budget_time"}:
                _, note = clamp_available_time(value)
                if note and note not in assumptions:
                    assumptions.append(note)
        info["summary"] = f"applied {len(clean)} answer(s): {', '.join(sorted(clean)) or 'none'}"
    return {"request": dump(request), "asked_before": True, "questions": [], "route": "parse_preferences",
            "extra_assumptions": assumptions}
