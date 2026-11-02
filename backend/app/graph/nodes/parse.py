"""parse_preferences: LLM structured output merged with the rules parser.

Requests without free text skip the LLM call entirely: their fields are authoritative and the rules parse
covers them (recorded in the trace as ``rules_reason: "structured_request"``; not a degradation).

The LLM may only add hard constraints (merge_parse unions them with the rules parse and the raw request).
Any failure (timeout, refusal, provider error, bad output, model not configured) falls back to the rules
parse with the degradation "AI parser unavailable; used rule-based parsing". The baseline never calls an LLM.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from langgraph.runtime import Runtime

from app.domain.constraints import is_valid
from app.domain.parser_rules import DEFAULT_MINUTES, DEFAULT_START_MIN, merge_parse, parse_rules
from app.domain.schemas import Constraint, Energy, ParseOutcome, PlanRequest, Preferences
from app.graph.nodes.common import DEADLINE_RESERVE_S, MIN_LLM_CALL_S, PARSER_DEGRADED, degrade, tracer_of
from app.graph.prompts import LLMParse, parse_hhmm, parse_messages
from app.graph.state import PlannerState, RunContext, dump, get_request
from app.llm.registry import (
    ModelRefusalError,
    RegistryError,
    build_structured_runnable,
    is_baseline,
    model_chain,
    parse_timeout_s,
)
from app.llm.usage import UsageTracker, empty_usage

log = logging.getLogger("app.graph")

_ISO = re.compile(r"^[A-Za-z]{3}$")


def llm_parse_to_outcome(parsed: LLMParse) -> ParseOutcome:
    """LLMParse -> ParseOutcome (source "llm"). Invalid values are dropped, never guessed."""
    start = parse_hhmm(parsed.start_time) if parsed.start_time else None
    if start is not None and start >= 1440:
        start = None
    minutes = parsed.available_minutes if parsed.available_minutes and 30 <= parsed.available_minutes <= 720 else None
    currency = parsed.currency.upper() if parsed.currency and _ISO.match(parsed.currency) else None
    constraints: list[Constraint] = []
    for c in parsed.hard_constraints:
        value = c.value.strip().lower().replace(" ", "_").replace("-", "_")
        con = Constraint(kind=c.kind, value=value)
        if is_valid(con) and con not in constraints:
            constraints.append(con)
    interests = [i.strip().lower()[:40] for i in parsed.interests if i and i.strip()][:10]
    prefs = Preferences(
        city=(parsed.city or "").strip()[:80] or None, budget=parsed.budget, currency=currency,
        start_min=start if start is not None else DEFAULT_START_MIN,
        available_minutes=minutes or DEFAULT_MINUTES, mood=(parsed.mood or "").strip()[:300],
        energy=Energy(parsed.energy), interests=interests, hard_constraints=constraints,
        soft_preferences=[s.strip()[:120] for s in parsed.soft_preferences if s and s.strip()][:10],
        neighbourhood=(parsed.neighbourhood or "").strip()[:80] or None, group_size=parsed.group_size or 1,
    )
    missing = [name for name, absent in (("city", prefs.city is None), ("budget", parsed.budget is None),
                                         ("available_time", minutes is None)) if absent]
    return ParseOutcome(preferences=prefs, missing=missing, source="llm")


def _user_text(request: PlanRequest) -> str:
    return " ".join([request.free_text or "", request.mood or "", *request.constraints, *request.interests]).lower()


def grounded_interest(tag: str, text: str) -> bool:
    """True when a word of the interest tag (its first five letters) appears in the user's own text."""
    words = [w for w in re.split(r"[\s_/-]+", tag.lower()) if len(w) >= 3]
    return any(w[:5] in text for w in words)


def ground_to_request(outcome: ParseOutcome, request: PlanRequest) -> ParseOutcome:
    """The LLM may only extract what the user wrote.

    * Constraint keys the user relaxed (``request.relaxed_constraints``) are never re-added.
    * Interests must be grounded in the user's text (a vague "something fun" must not become "shopping").
    * Without free text there is nothing to extract from, so fields the request left empty stay empty instead
      of taking invented values (e.g. a start time)."""
    p = outcome.preferences
    relaxed = set(request.relaxed_constraints)
    text = _user_text(request)
    p = p.model_copy(update={
        "hard_constraints": [c for c in p.hard_constraints if c.key not in relaxed],
        "interests": [i for i in p.interests if grounded_interest(i, text)],
    })
    outcome = outcome.model_copy(update={"preferences": p})
    if (request.free_text or "").strip():
        return outcome
    missing = set(outcome.missing)
    updates: dict[str, Any] = {}
    if not request.city:
        updates["city"] = None
        missing.add("city")
    if request.budget is None:
        updates.update(budget=None, currency=None)
        missing.add("budget")
    if not request.start_time:
        updates["start_min"] = DEFAULT_START_MIN
    if request.available_time is None:
        updates["available_minutes"] = DEFAULT_MINUTES
        missing.add("available_time")
    if not (request.mood or "").strip():
        updates.update(mood="", energy=Energy.MEDIUM)
    if not request.interests:
        updates["interests"] = []
    if not request.neighbourhood:
        updates["neighbourhood"] = None
    if request.group_size == 1:
        updates["group_size"] = 1
    order = ("city", "budget", "available_time")
    return outcome.model_copy(update={"preferences": p.model_copy(update=updates),
                                      "missing": [m for m in order if m in missing]})


async def llm_parse(request: PlanRequest, rc: RunContext, model_id: str
                    ) -> tuple[ParseOutcome | None, dict[str, Any], str | None, str | None]:
    """(outcome or None, usage, model that answered, failure kind)."""
    settings = rc.deps.settings
    timeout = min(parse_timeout_s(model_id), rc.remaining() - DEADLINE_RESERVE_S)
    if timeout < MIN_LLM_CALL_S:
        return None, empty_usage(), None, "no_time"
    tracker = UsageTracker()
    try:
        chain = model_chain(settings, model_id)
        runnable = build_structured_runnable(chain, LLMParse, settings,
                                             primary_timeout_s=timeout * 0.6 if len(chain) > 1 else None)
        out = await asyncio.wait_for(
            runnable.ainvoke(parse_messages(request, rc.deps.now()), config={"callbacks": [tracker]}), timeout)
        parsed = out["parsed"]
        if not isinstance(parsed, LLMParse):
            parsed = LLMParse.model_validate(parsed)
        outcome = ground_to_request(llm_parse_to_outcome(parsed), request)
        return outcome, tracker.usage, str(out.get("model_id") or model_id), None
    except TimeoutError:
        kind = "timeout"
    except ModelRefusalError:
        kind = "refusal"
    except RegistryError:
        kind = "not_configured"
    except Exception as exc:  # provider SDK errors vary; any failure means "use the rules parse"
        kind = type(exc).__name__
        log.warning("LLM parse failed", extra={"error_type": kind, "model": model_id})
    return None, tracker.usage, None, kind


async def parse_preferences(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    request = get_request(state)
    model_id = state.get("model_id") or rc.model_id
    usage = empty_usage()
    degradations: list[str] = []
    with tracer.node("parse_preferences") as info:
        rules = parse_rules(request, rc.deps.now())
        llm_outcome: ParseOutcome | None = None
        answered: str | None = None
        # Structured requests need no LLM: the fields are authoritative and the rules parse covers them, which
        # saves a model call (the production model allows only a few requests per minute).
        structured = not (request.free_text or "").strip()
        rules_reason = "baseline" if is_baseline(model_id) else "structured_request" if structured else None
        if rules_reason is None:
            llm_outcome, usage, answered, failure = await llm_parse(request, rc, model_id)
            if failure is not None:
                degradations = degrade(tracer, "parse_preferences", PARSER_DEGRADED, reason=failure)
        outcome = merge_parse(llm_outcome, rules, request)
        prefs = outcome.preferences
        why = {"structured_request": " (structured request, no LLM needed)", "baseline": " (no-LLM baseline)"}
        info["summary"] = (f"{outcome.source} parse{why.get(rules_reason or '', '')}: city={prefs.city or '?'}, "
                           f"energy={prefs.energy.value}, {len(prefs.hard_constraints)} hard constraint(s)")
        info["data"] = {"source": outcome.source, "model": answered, "missing": outcome.missing,
                        "rules_reason": rules_reason}
    return {"parse": dump(outcome), "usage": usage, "degradations": degradations, "route": "resolve_location"}
