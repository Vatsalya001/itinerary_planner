"""build_context: final preferences (visible defaults), weather, time window, pacing and enforced constraints."""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from app.domain.parser_rules import apply_defaults
from app.graph.nodes.common import AGENT_NO_TIME, DEADLINE_RESERVE_S, WEATHER_ASSUMPTION, tracer_of
from app.graph.prompts import hhmm
from app.graph.state import PlannerState, RunContext, dump, get_matches, get_parse, get_request
from app.llm.registry import is_baseline
from app.pipeline import build_context as pipeline_build_context
from app.pipeline import enforced_constraints

# Below this many seconds the agent would not get a meaningful turn.
MIN_AGENT_WINDOW_S = DEADLINE_RESERVE_S + 3


async def build_context(state: PlannerState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    rc = runtime.context
    tracer = tracer_of(runtime)
    model_id = state.get("model_id") or rc.model_id
    matches = get_matches(state) or []
    location = matches[0]
    degradations: list[str] = []
    extra: list[str] = []
    with tracer.node("build_context") as info:
        prefs = apply_defaults(get_parse(state), location)
        ctx, ctx_degradations = await pipeline_build_context(prefs, location, rc.deps, tracer, rc.deps.now())
        enforced, original = enforced_constraints(prefs, get_request(state))
        info["summary"] = (f"{location.name}, {ctx.date.isoformat()} {hhmm(ctx.start_min)}-{hhmm(ctx.end_min)}; "
                           f"{ctx.weather.summary or ctx.weather.status}; {prefs.energy.value} energy; "
                           f"{len(enforced)} hard constraint(s)")
        info["data"] = {"location_id": location.id, "timezone": location.timezone, "currency": prefs.currency}
    if ctx.weather.status != "ok":
        tracer.emit("degraded", node="get_weather", summary="Weather not checked; planning without a forecast",
                    data={"tool": "get_weather", "recovered": True})
        degradations += ctx_degradations
        extra.append(WEATHER_ASSUMPTION)
    route = "plan_agent"
    agent_stop = None
    if is_baseline(model_id):
        route = "fallback_planner"
    elif rc.remaining() < MIN_AGENT_WINDOW_S:
        agent_stop = AGENT_NO_TIME  # reported by fallback_planner
        route = "fallback_planner"
    return {
        "agent_stop": agent_stop,
        "prefs": dump(prefs), "context": dump(ctx), "enforced": [dump(c) for c in enforced],
        "original": [dump(c) for c in original], "degradations": degradations, "extra_assumptions": extra,
        "route": route,
    }
