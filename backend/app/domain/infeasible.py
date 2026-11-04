"""Explain why no plan fits and propose one-tap relaxations. OWNER: domain agent.

Searches (all with the deterministic planner, app.domain.fallback):

* cheapest_budget: the lowest worst-case cost over valid plans with the budget ignored (cheapest-first beam).
* min_minutes: the shortest elapsed time over valid plans with the window relaxed to 600 minutes.
* one drop test per hard constraint: does a plan exist without it?
* meal timing: if the plan only works with a sit-down meal outside meal times, say so and offer a start time
  that lets the meal land in its window (and then never offer to drop a diet or accessibility need);
* pacing rules (seated stop, walking limit, long rides, stop count) relaxed one at a time, so the reason names
  the energy-profile rule that binds; for a low-energy day, a one-tap "moderate pace" relaxation when that works;
* if nothing single works: budget and time relaxed together, then (if the start time is not already 15:00)
  the default 15:00 start. The "even with an unlimited budget and a longer window" sentence is only used when
  those searches actually ran and failed.

Relaxation patches use PlanRequest field names: ``budget`` (and ``currency``), ``available_time`` (minutes),
``start_time``, ``mood`` and ``relaxed_constraints`` (the one constraint key being dropped, e.g.
``["access:wheelchair"]``; the client unions it with keys already relaxed, and the parser excludes relaxed keys
from every source, free text included).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.categories import currency_symbol
from app.domain.constraints import merge_constraints
from app.domain.evidence import MEAL_EARLY_MIN, candidate_allowed
from app.domain.fallback import SearchResult, search_plans
from app.domain.pacing import MEAL_WINDOWS, meal_windows_between, profile_for
from app.domain.schedule import plan_currency
from app.domain.schemas import (
    Candidate,
    Constraint,
    Energy,
    InfeasibleReport,
    PacingProfile,
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


def _hhmm(minutes: int) -> str:
    h, m = divmod(minutes % (24 * 60), 60)
    return f"{h:02d}:{m:02d}"


def _shifted(ctx: WorldContext, prefs: Preferences, start: int) -> tuple[WorldContext, Preferences]:
    end = start + prefs.available_minutes
    new_ctx = ctx.model_copy(update={"start_min": start, "end_min": end, "meal_windows": meal_windows_between(start, end)})
    return new_ctx, prefs.model_copy(update={"start_min": start})


def _meal_aligned_starts(ctx: WorldContext) -> list[int]:
    """Start times near the requested one that put a main meal window at (or just after) the start."""
    starts: set[int] = set()
    for w in MEAL_WINDOWS:
        if w.name == "snack":
            continue
        for t in (w.start_min - MEAL_EARLY_MIN, w.start_min):
            if t != ctx.start_min and abs(t - ctx.start_min) <= 4 * 60:
                starts.add(t)
    return sorted(starts, key=lambda t: (abs(t - ctx.start_min), t))


def explain_infeasible(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                       constraints: list[Constraint], travel: TravelEstimator) -> InfeasibleReport:
    """Which constraint removed what; cheapest feasible budget and minimum minutes found by re-running the
    deterministic planner with budget/time relaxed; Relaxation patches use PlanRequest field names."""
    enforced = merge_constraints(prefs.hard_constraints, list(constraints))
    sym = currency_symbol(plan_currency(ctx, prefs, candidates))
    unique = list({c.id: c for c in candidates}.values())

    if not unique:
        return InfeasibleReport(
            reason=f"No suitable place data is available for {ctx.location.name}. Try another city or retry the search.",
        )

    baseline = search_plans(unique, ctx, prefs, enforced, travel)
    if baseline is not None:
        return InfeasibleReport(reason="A valid plan fits these settings; nothing needs relaxing.",
                                cheapest_budget=_worst(search_plans(unique, ctx, prefs, enforced, travel,
                                                                    objective="cheapest", ignore_budget=True)),
                                min_minutes=_elapsed(baseline))

    mandatory = [r for r in prefs.venue_requirements if r.mandatory]
    if mandatory:
        return InfeasibleReport(
            reason="No verified plan fits the mandatory venue facilities and your other settings. "
                   "Missing evidence does not mean the facility is absent. Confirm directly with venues, "
                   "or explicitly make the facility optional.",
            binding_constraints=[f"requirement:{r.id}" for r in mandatory],
            relaxations=[Relaxation(label=f"Make {r.description} optional (not guaranteed)",
                                   patch={"relaxed_constraints": [f"requirement:{r.id}"]}) for r in mandatory])

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
                                      patch={"budget": cheapest_budget, "currency": plan_currency(ctx, prefs)}))
    if min_minutes is not None and min_minutes > prefs.available_minutes:
        binding.append("time")
        relaxations.append(Relaxation(label=f"Allow {fmt_duration(min_minutes)}",
                                      patch={"available_time": min_minutes}))

    # meal timing: sit-down meals only go in breakfast/lunch/dinner (or just before); is that what binds?
    meal_sentence = None
    meal_start: int | None = None
    if search_plans(unique, ctx, prefs, enforced, travel, meal_timing=False) is not None:
        binding.append("meal_timing")
        for t in _meal_aligned_starts(ctx):
            m_ctx, m_prefs = _shifted(ctx, prefs, t)
            if search_plans(unique, m_ctx, m_prefs, enforced, travel) is not None:
                meal_start = t
                break
        if meal_start is not None:
            meal_sentence = (f"The options that fit include a sit-down meal, and meals are planned inside meal times; "
                             f"starting at {fmt_clock(meal_start)} lets it land at a proper time.")
            relaxations.append(Relaxation(label=f"Start at {fmt_clock(meal_start)}",
                                          patch={"start_time": _hhmm(meal_start)}))
        else:
            meal_sentence = ("The options that fit include a sit-down meal, but no meal time (lunch from 12:00 PM, "
                             "dinner from 7:30 PM) falls inside your window.")

    drop_labels: list[str] = []
    for con in enforced:
        if meal_start is not None and con.kind in {"diet", "access"}:
            continue  # timing is what binds: never suggest giving up a diet or accessibility need
        remaining = [c for c in enforced if c != con]
        relaxed = prefs.model_copy(update={"hard_constraints": remaining})
        if search_plans(unique, ctx, relaxed, remaining, travel) is not None:
            binding.append(con.key)
            label = con.source_text.strip() or constraint_label(con)
            drop_labels.append(label)
            relaxations.append(Relaxation(label=f"Drop '{label}'",
                                          patch={"relaxed_constraints": [con.key]}))

    # pacing rules (energy profile): test each on its own so the reason names the one that binds
    pacing = ctx.pacing
    energy = pacing.energy.value
    pacing_sentences: list[str] = []
    rule_tests: list[tuple[str, PacingProfile, str]] = []
    if pacing.min_seated_stops:
        rule_tests.append(("seated", pacing.model_copy(update={"min_seated_stops": 0}),
                           f"A {energy}-energy day needs at least one seated stop, and no seated option fits the rest "
                           "of your requirements."))
    rule_tests.append(("walking", pacing.model_copy(update={"max_walk_minutes": RELAXED_MINUTES}),
                       f"The {energy}-energy walking limit of {pacing.max_walk_minutes} min is too tight for these "
                       "options."))
    rule_tests.append(("long_legs", pacing.model_copy(update={"max_long_legs": 10}),
                       f"A {energy}-energy day allows at most {pacing.max_long_legs} longer ride(s), and the options "
                       "are spread out."))
    rule_tests.append(("stops", pacing.model_copy(update={"max_stops": 6}),
                       f"A {energy}-energy day allows at most {pacing.max_stops} stops, too few for everything you "
                       "asked to include."))
    for key, relaxed_pacing, sentence in rule_tests:
        if search_plans(unique, ctx, prefs, enforced, travel, pacing=relaxed_pacing) is not None:
            binding.append(f"pacing:{key}")
            pacing_sentences.append(sentence)
    medium = profile_for(Energy.MEDIUM)
    if (pacing.energy == Energy.LOW and pacing_sentences
            and search_plans(unique, ctx, prefs, enforced, travel, pacing=medium) is not None):
        relaxations.append(Relaxation(label="Plan at a moderate pace",
                                      patch={"mood": "feeling okay, happy with a moderate pace"}))

    joint_budget: int | None = None
    joint_minutes: int | None = None
    joint_tested = False
    if not relaxations:
        joint_tested = True
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
    if ctx.start_min != DEFAULT_START_MIN and meal_start is None:
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
    if meal_sentence:
        sentences.append(meal_sentence)
    sentences.extend(pacing_sentences)
    if joint_tested and not pacing_sentences and not meal_sentence and cheapest_budget is None \
            and min_minutes is None and joint_budget is None:
        # all three searches ran: budget unlimited, window relaxed, and both together
        sentences.append("Even with an unlimited budget and a longer window, nothing found fits this start time "
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
