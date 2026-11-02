"""StateGraph assembly. Nodes set ``state["route"]``; conditional edges follow it.

    guard_input -> parse_preferences -> resolve_location -> check_clarity
    check_clarity -> ask_clarification (interrupt) -> parse_preferences | build_context | finalize
    build_context -> plan_agent | fallback_planner (baseline, or no time left)
    plan_agent -> tools | plan_agent (one nudge) | fallback_planner
    tools -> plan_agent | validate_plan (submit_plan) | fallback_planner
    validate_plan -> finalize | plan_agent (repair) | fallback_planner
    fallback_planner -> finalize | explain_infeasible -> finalize -> END
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.agent import plan_agent
from app.graph.nodes.clarify import GEOCODER_RETRY, ask_clarification, check_clarity, resolve_location
from app.graph.nodes.context import build_context
from app.graph.nodes.execute import run_tools
from app.graph.nodes.fallback import explain_infeasible_node, fallback_planner
from app.graph.nodes.finalize import finalize
from app.graph.nodes.guard import guard_input
from app.graph.nodes.parse import parse_preferences
from app.graph.nodes.validate import validate_submission
from app.graph.state import PlannerState, RunContext


def follow_route(state: PlannerState) -> str:
    return state["route"]


def build_graph() -> StateGraph[Any, Any, Any, Any]:
    g: StateGraph[Any, Any, Any, Any] = StateGraph(PlannerState, context_schema=RunContext)
    g.add_node("guard_input", guard_input)
    g.add_node("parse_preferences", parse_preferences)
    g.add_node("resolve_location", resolve_location, retry_policy=GEOCODER_RETRY)
    g.add_node("check_clarity", check_clarity)
    g.add_node("ask_clarification", ask_clarification)
    g.add_node("build_context", build_context)
    g.add_node("plan_agent", plan_agent)
    g.add_node("tools", run_tools)
    g.add_node("validate_plan", validate_submission)
    g.add_node("fallback_planner", fallback_planner)
    g.add_node("explain_infeasible", explain_infeasible_node)
    g.add_node("finalize", finalize)

    g.add_edge(START, "guard_input")
    g.add_edge("guard_input", "parse_preferences")
    g.add_edge("parse_preferences", "resolve_location")
    g.add_edge("resolve_location", "check_clarity")
    g.add_conditional_edges("check_clarity", follow_route, ["ask_clarification", "build_context", "finalize"])
    g.add_edge("ask_clarification", "parse_preferences")
    g.add_conditional_edges("build_context", follow_route, ["plan_agent", "fallback_planner"])
    g.add_conditional_edges("plan_agent", follow_route, ["tools", "plan_agent", "fallback_planner"])
    g.add_conditional_edges("tools", follow_route, ["plan_agent", "validate_plan", "fallback_planner"])
    g.add_conditional_edges("validate_plan", follow_route, ["finalize", "plan_agent", "fallback_planner"])
    g.add_conditional_edges("fallback_planner", follow_route, ["finalize", "explain_infeasible"])
    g.add_edge("explain_infeasible", "finalize")
    g.add_edge("finalize", END)
    return g


def recursion_limit(max_agent_turns: int, max_submit_attempts: int) -> int:
    """Upper bound on graph steps: preamble + clarification loop + two steps per agent turn + repairs."""
    return 30 + 3 * max_agent_turns + 2 * max_submit_attempts
