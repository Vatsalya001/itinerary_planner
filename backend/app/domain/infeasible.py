"""Explain why no plan fits and propose one-tap relaxations. OWNER: domain agent.

Searches (all with the deterministic planner, app.domain.fallback):

* cheapest_budget: the lowest worst-case cost over valid plans with the budget ignored (cheapest-first beam).
* min_minutes: the shortest elapsed time over valid plans with the window relaxed to 600 minutes.
* one drop test per hard constraint: does a plan exist without it?
* if nothing single works: budget and time relaxed together, then (if the start time is not already 15:00)
  the default 15:00 start.

Relaxation patches use PlanRequest field names: ``budget``, ``available_time`` (minutes), ``constraints``
(remaining constraint texts) and ``start_time``.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.categories import currency_symbol
from app.domain.constraints import merge_constraints
from app.domain.evidence import candidate_allowed
from app.domain.fallback import SearchResult, search_plans
from app.domain.pacing import meal_windows_between
from app.domain.schedule import plan_currency
from app.domain.schemas import (
    Candidate,
    Constraint,
    InfeasibleReport,
    Preferences,
    Relaxation,
    WorldContext,
)
from app.domain.timeutil import fmt_clock, fmt_duration
from app.providers.base import TravelEstimator

RELAXED_MINUTES = 600
DEFAULT_START_MIN = 15 * 60

_INCLUDE_LABELS = {
    "sit_down_meal": "include a sit-down meal", "outdoor_walk": "include an outdoor walk",
    "live_music": "include live music", "coffee": "include coffee", "museum": "include a museum",
    "art": "include art", "books": "include books", "park": "include a park", "event": "include an event",
    "shopping": "include shopping", "dessert": "include dessert",
}
_AVOID_LABELS = {
    "mall": "avoid malls", "bar": "avoid bars", "brewery": "avoid breweries", "market": "avoid markets",
    "cinema": "avoid cinemas", "shopping": "avoid shopping", "alcohol": "avoid alcohol",
}
_LABELS = {
    "diet:vegetarian": "vegetarian", "diet:vegan": "vegan", "diet:jain": "Jain", "alcohol:avoid": "no alcohol venues",
    "access:wheelchair": "wheelchair accessible", "crowd:avoid": "avoid crowded places",
    "setting:indoors_only": "indoors only", "setting:outdoors_only": "outdoors only",
}
_RULES_OUT = {
    "diet": "Requiring confirmed {label} food",
    "access": "Requiring confirmed wheelchair access",
    "alcohol": "Avoiding alcohol venues",
    "crowd": "Avoiding crowds",
    "setting": "Keeping to '{label}'",
    "must_avoid": "'{label}'",
}


def constraint_label(c: Constraint) -> str:
    """Short human label that the rules parser maps back to the same constraint where possible."""
    if c.kind == "must_include":
        return _INCLUDE_LABELS.get(c.value, f"include {c.value.replace('_', ' ')}")
    if c.kind == "must_avoid":
        return _AVOID_LABELS.get(c.value, f"avoid {c.value.replace('_', ' ')}")
    return _LABELS.get(c.key, c.value.replace("_", " "))


def _remaining_texts(remaining: list[Constraint], dropped: Constraint) -> list[str]:
    texts: list[str] = []
    for c in remaining:
        text = c.source_text.strip()
        if not text or (dropped.source_text and text == dropped.source_text.strip()):
            text = constraint_label(c)
        if text not in texts:
            texts.append(text)
    return texts


def _worst(result: SearchResult | None) -> int | None:
    if result is None or result.report.plan is None:
        return None
    return result.report.plan.totals.worst_case


def _elapsed(result: SearchResult | None) -> int | None:
    if result is None or result.report.plan is None:
        return None
    return result.report.plan.totals.elapsed_min


def _relaxed_time(ctx: WorldContext, prefs: Preferences, start_min: int | None = None) -> tuple[WorldContext, Preferences]:
    start = ctx.start_min if start_min is None else start_min
    end = start + RELAXED_MINUTES
    new_ctx = ctx.model_copy(update={"start_min": start, "end_min": end, "meal_windows": meal_windows_between(start, end)})
    new_prefs = prefs.model_copy(update={"available_minutes": RELAXED_MINUTES, "start_min": start})
    return new_ctx, new_prefs


def explain_infeasible(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                       constraints: list[Constraint], travel: TravelEstimator) -> InfeasibleReport:
    """Which constraint removed what; cheapest feasible budget and minimum minutes found by re-running the
    deterministic planner with budget/time relaxed; Relaxation patches use PlanRequest field names."""
    enforced = merge_constraints(prefs.hard_constraints, list(constraints))
    sym = currency_symbol(plan_currency(ctx, prefs, candidates))
    unique = list({c.id: c for c in candidates}.values())

    if not unique:
        return InfeasibleReport(
            reason=f"No places or events were found for {ctx.location.name}, so no plan could be built.",
        )

    baseline = search_plans(unique, ctx, prefs, enforced, travel)
    if baseline is not None:
        return InfeasibleReport(reason="A valid plan fits these settings; nothing needs relaxing.",
                                cheapest_budget=_worst(search_plans(unique, ctx, prefs, enforced, travel,
                                                                    objective="cheapest", ignore_budget=True)),
                                min_minutes=_elapsed(baseline))

    # what each constraint rules out on its own
    removed = {c.key: sum(1 for cand in unique if not candidate_allowed(cand, [c], ctx)[0]) for c in enforced}

    cheapest = search_plans(unique, ctx, prefs, enforced, travel, objective="cheapest", ignore_budget=True)
    cheapest_budget = _worst(cheapest)
    t_ctx, t_prefs = _relaxed_time(ctx, prefs)
    shortest = search_plans(unique, t_ctx, t_prefs, enforced, travel, objective="shortest")
    min_minutes = _elapsed(shortest)

    relaxations: list[Relaxation] = []
    binding: list[str] = []

    budget = prefs.budget
    if budget is not None and cheapest_budget is not None and cheapest_budget > budget:
        binding.append("budget")
        relaxations.append(Relaxation(label=f"Raise budget to {sym}{cheapest_budget}",
                                      patch={"budget": cheapest_budget}))
    if min_minutes is not None and min_minutes > prefs.available_minutes:
        binding.append("time")
        relaxations.append(Relaxation(label=f"Allow {fmt_duration(min_minutes)}",
                                      patch={"available_time": min_minutes}))

    drop_labels: list[str] = []
    for con in enforced:
        remaining = [c for c in enforced if c != con]
        relaxed = prefs.model_copy(update={"hard_constraints": remaining})
        if search_plans(unique, ctx, relaxed, remaining, travel) is not None:
            binding.append(con.key)
            label = con.source_text.strip() or constraint_label(con)
            drop_labels.append(label)
            relaxations.append(Relaxation(label=f"Drop '{label}'",
                                          patch={"constraints": _remaining_texts(remaining, con)}))

    joint_budget: int | None = None
    joint_minutes: int | None = None
    if not relaxations:
        joint = search_plans(unique, t_ctx, t_prefs, enforced, travel, objective="cheapest", ignore_budget=True)
        if joint is not None and joint.report.plan is not None:
            joint_budget = joint.report.plan.totals.worst_case
            joint_minutes = joint.report.plan.totals.elapsed_min
            patch: dict[str, int] = {}
            parts = []
            if budget is not None and joint_budget > budget:
                patch["budget"] = joint_budget
                parts.append(f"raise budget to {sym}{joint_budget}")
                binding.append("budget")
            if joint_minutes > prefs.available_minutes:
                patch["available_time"] = joint_minutes
                parts.append(f"allow {fmt_duration(joint_minutes)}")
                binding.append("time")
            if patch:
                relaxations.append(Relaxation(label=(" and ".join(parts)).capitalize(), patch=dict(patch)))
    if not relaxations and ctx.start_min != DEFAULT_START_MIN:
        s_ctx = ctx.model_copy(update={
            "start_min": DEFAULT_START_MIN, "end_min": DEFAULT_START_MIN + prefs.available_minutes,
            "meal_windows": meal_windows_between(DEFAULT_START_MIN, DEFAULT_START_MIN + prefs.available_minutes),
        })
        s_prefs = prefs.model_copy(update={"start_min": DEFAULT_START_MIN})
        if search_plans(unique, s_ctx, s_prefs, enforced, travel) is not None:
            binding.append("start_time")
            relaxations.append(Relaxation(label=f"Start at {fmt_clock(DEFAULT_START_MIN)}",
                                          patch={"start_time": "15:00"}))

    if not binding:
        binding = [k for k, n in sorted(removed.items(), key=lambda kv: -kv[1]) if n > 0][:3]

    # reason text (numbers here are computed by code)
    sentences = ["No plan meets every requirement."]
    if "budget" in binding and cheapest_budget is not None and budget is not None:
        sentences.append(f"The cheapest valid plan costs {sym}{cheapest_budget} in the worst case, above your "
                         f"{sym}{budget} budget.")
    if "time" in binding and min_minutes is not None:
        sentences.append(f"The shortest valid plan needs {fmt_duration(min_minutes)}, more than the "
                         f"{fmt_duration(prefs.available_minutes)} you have.")
    if cheapest_budget is None and min_minutes is None and joint_budget is None:
        sentences.append("Even with an unlimited budget or a longer window, nothing found fits this start time "
                         "and every hard constraint.")
    if "start_time" in binding:
        sentences.append(f"Starting at {fmt_clock(DEFAULT_START_MIN)} would make a plan possible.")
    by_impact = sorted((c for c in enforced if removed.get(c.key, 0) > 0), key=lambda c: -removed[c.key])[:2]
    for c in by_impact:
        template = _RULES_OUT.get(c.kind)
        if template:
            lead = template.format(label=constraint_label(c))
            sentences.append(f"{lead} rules out {removed[c.key]} of {len(unique)} places.")
    if drop_labels:
        sentences.append("Dropping " + " or ".join(f"'{d}'" for d in drop_labels[:2]) + " would make a plan possible.")

    return InfeasibleReport(
        reason=" ".join(sentences),
        binding_constraints=list(dict.fromkeys(binding)),
        relaxations=relaxations,
        cheapest_budget=cheapest_budget if cheapest_budget is not None else joint_budget,
        min_minutes=min_minutes if min_minutes is not None else joint_minutes,
    )
