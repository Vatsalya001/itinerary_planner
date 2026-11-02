"""Deterministic planner (no LLM): prefilter, score, beam search, validate. OWNER: domain agent.

Search: candidates are prefiltered (constraints with positive evidence, city radius, sources, event date,
opening hours inside the window, budget), scored (app.domain.scoring), and a beam search (width
``BEAM_WIDTH``) builds ordered sequences up to the pacing profile's max stops. Each extension is simulated
with the same arithmetic as the validator (legs from the TravelEstimator, buffers, opening hours, crowds,
daylight, walking, long legs, budget with contingency, time). Complete sequences (must_include met, enough
seated stops) are ranked by quality (interest coverage, candidate scores, meal timing, time use, travel),
durations are stretched toward each stop's maximum while time remains, an indoor Plan B is added for
outdoor stops when rain is likely, and finalists are checked with ``validate_plan`` until one passes.

The same search runs in "cheapest" and "shortest" modes for app.domain.infeasible.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.domain.constraints import merge_constraints
from app.domain.costs import worst_case
from app.domain.evidence import (
    candidate_allowed,
    category_info,
    event_window,
    is_food_stop,
    is_meal_stop,
    is_outdoor,
    is_walking_stop,
    likely_busy,
    open_overlap_minutes,
    opening_status,
    satisfies_include,
)
from app.domain.schemas import (
    Candidate,
    Constraint,
    DraftStop,
    Energy,
    Leg,
    MealWindow,
    Omission,
    PlanDraft,
    Preferences,
    ValidationReport,
    WorldContext,
)
from app.domain.scoring import build_inputs, covered_interests, safe_interest_label, score_candidate
from app.domain.travel import coords_valid, haversine_km
from app.domain.validator import (
    CITY_RADIUS_KM,
    duration_bounds,
    numeric_claims,
    plan_b_problems,
    source_problems,
    validate_plan,
)
from app.providers.base import TravelEstimator

BEAM_WIDTH = 30
POOL_LIMIT = 40
POOL_PER_NEED = 5
FINALISTS = 25
MAX_VALIDATIONS = 12

Objective = Literal["best", "cheapest", "shortest"]

DIET_LABEL = {"vegetarian": "vegetarian", "vegan": "vegan", "jain": "Jain"}

OMIT_REASONS: dict[str, str] = {
    "diet": "Left out because its menu isn't confirmed to suit your diet.",
    "access": "Left out because wheelchair access isn't confirmed.",
    "alcohol": "Left out because it's an alcohol-focused venue.",
    "setting": "Left out because it doesn't match your indoor or outdoor preference.",
    "must_avoid": "Left out because it's a kind of place you asked to avoid.",
    "crowd": "Left out because it's likely busy for most of the day.",
    "budget": "Left out because it alone would strain the budget.",
    "closed": "Left out because it isn't open long enough during your window.",
    "far": "Left out because it's outside the city area.",
    "source": "Left out because its link couldn't be verified.",
    "event_date": "Left out because the event isn't on your date or inside your window.",
    "coords": "Left out because its location is missing.",
    "category": "Left out because its type couldn't be recognised.",
}


# ---------------------------------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------------------------------


def prefilter(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
              constraints: list[Constraint], *, budget: int | None) -> tuple[list[Candidate], dict[str, list[str]]]:
    """(eligible candidates, {excluded id: [reason keys]}). Reason keys index OMIT_REASONS; constraint
    exclusions use the constraint kind."""
    kept: list[Candidate] = []
    excluded: dict[str, list[str]] = {}
    seen: set[str] = set()
    for c in candidates:
        if c.id in seen:
            continue
        seen.add(c.id)
        reasons: list[str] = []
        if category_info(c) is None:
            reasons.append("category")
        if not coords_valid(c.lat, c.lng):
            reasons.append("coords")
        elif haversine_km(c.lat, c.lng, ctx.location.lat, ctx.location.lng) > CITY_RADIUS_KM:
            reasons.append("far")
        if source_problems(c):
            reasons.append("source")
        lo, _ = duration_bounds(c)
        if c.kind == "event":
            window = event_window(c)
            if (c.event_date != ctx.date or window is None or window[0] < ctx.start_min
                    or window[0] + lo > ctx.end_min):
                reasons.append("event_date")
        else:
            overlap = open_overlap_minutes(c, ctx.start_min, ctx.end_min)
            if overlap is not None and overlap < lo:
                reasons.append("closed")
        if budget is not None and worst_case(c.cost.max, 0) > budget:
            reasons.append("budget")
        if not reasons:
            for con in constraints:
                ok, _why = candidate_allowed(c, [con], ctx)
                if not ok and con.kind not in reasons:
                    reasons.append(con.kind)
        if reasons:
            excluded[c.id] = reasons
        else:
            kept.append(c)
    return kept, excluded


# ---------------------------------------------------------------------------------------------------
# Search state
# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Node:
    idx: tuple[int, ...]
    durs: tuple[int, ...]
    starts: tuple[int, ...]
    end: int
    cost_max: int
    walk: int
    long_legs: int
    seated: int
    leg_minutes: int
    covered: frozenset[str]
    includes: frozenset[str]
    base: float
    meal_hit: bool
    cats: tuple[str, ...]


@dataclass
class SearchResult:
    draft: PlanDraft
    report: ValidationReport


class Planner:
    """Reusable search over one candidate set. ``ignore_budget`` / relaxed contexts are used by infeasible."""

    def __init__(self, candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                 constraints: list[Constraint], travel: TravelEstimator, *, ignore_budget: bool = False) -> None:
        self.ctx = ctx
        self.prefs = prefs
        self.travel = travel
        self.constraints = merge_constraints(prefs.hard_constraints, list(constraints))
        self.validation_prefs = prefs.model_copy(update={"budget": None}) if ignore_budget else prefs
        self.budget = None if ignore_budget else prefs.budget
        self.ledger: dict[str, Candidate] = {}
        for c in candidates:
            self.ledger.setdefault(c.id, c)
        self.limit = min(prefs.available_minutes, ctx.end_min - ctx.start_min)
        self.pacing = ctx.pacing
        keys = {c.key for c in self.constraints}
        self.crowd_avoid = "crowd:avoid" in keys
        self.includes = tuple(c.value for c in self.constraints if c.kind == "must_include")
        self.sunset = ctx.weather.sunset_min
        self.eligible, self.excluded = prefilter(list(self.ledger.values()), ctx, prefs, self.constraints,
                                                 budget=self.budget)
        self.inputs = build_inputs(self.eligible, ctx, prefs, self.constraints)
        self.scores = {c.id: score_candidate(c, self.inputs) for c in self.eligible}
        self.pool = self._select_pool()
        self.pool_scores = [self.scores[c.id] for c in self.pool]
        self.pool_covered = [frozenset(covered_interests(c, self.inputs.interests)) for c in self.pool]
        self.pool_includes = [frozenset(v for v in self.includes if satisfies_include(c, v)) for c in self.pool]
        self.meal_targets: tuple[MealWindow, ...] = tuple(
            w for w in ctx.meal_windows
            if min(w.end_min, ctx.end_min) - max(w.start_min, ctx.start_min) >= 30
        )
        self._legs: dict[tuple[int, int], Leg | None] = {}

    # -- pool -----------------------------------------------------------------------------------------

    def _select_pool(self) -> list[Candidate]:
        ranked = sorted(self.eligible, key=lambda c: (-self.scores[c.id], c.id))
        chosen: dict[str, Candidate] = {c.id: c for c in ranked[:POOL_LIMIT]}

        def add_top(pred: Callable[[Candidate], bool]) -> None:
            n = 0
            for c in ranked:
                if pred(c):
                    chosen.setdefault(c.id, c)
                    n += 1
                    if n >= POOL_PER_NEED:
                        break

        def includes(value: str) -> Callable[[Candidate], bool]:
            return lambda c: satisfies_include(c, value)

        def covers(interest: str) -> Callable[[Candidate], bool]:
            return lambda c: bool(covered_interests(c, [interest]))

        for value in self.includes:
            add_top(includes(value))
        add_top(is_meal_stop)
        if self.pacing.min_seated_stops:
            add_top(lambda c: c.seated)
        for interest in self.inputs.interests:
            add_top(covers(interest))
        return sorted(chosen.values(), key=lambda c: (-self.scores[c.id], c.id))

    # -- simulation -----------------------------------------------------------------------------------

    def _leg(self, i: int, j: int) -> Leg | None:
        key = (i, j)
        if key not in self._legs:
            try:
                self._legs[key] = self.travel.leg(self.pool[i], self.pool[j], self.ctx)
            except ValueError:
                self._legs[key] = None
        return self._legs[key]

    def _duration_options(self, c: Candidate, objective: Objective) -> list[int]:
        lo, hi = duration_bounds(c)
        if c.kind == "event":
            options = [hi, lo]
        else:
            default = min(max(c.duration_range[1], lo), hi)
            options = [default, lo] if objective == "best" else [lo]
        return list(dict.fromkeys(options))

    def _meal_hit(self, c: Candidate, start: int, end: int) -> bool:
        for w in self.meal_targets:
            if start < w.end_min and w.start_min < end:
                if w.name == "snack" and is_food_stop(c):
                    return True
                if w.name != "snack" and is_meal_stop(c):
                    return True
        return False

    def _extend(self, node: _Node | None, j: int, dur: int) -> _Node | None:
        ctx, pacing = self.ctx, self.pacing
        c = self.pool[j]
        leg: Leg | None = None
        if node is None:
            arrival = ctx.start_min
        else:
            if j in node.idx or len(node.idx) >= pacing.max_stops:
                return None
            leg = self._leg(node.idx[-1], j)
            if leg is None:
                return None
            arrival = node.end + leg.minutes_max + pacing.buffer_minutes
        if c.kind == "event" and c.event_start_min is not None:
            if arrival > c.event_start_min:
                return None
            start = c.event_start_min
        else:
            start = arrival
        end = start + dur
        if end - ctx.start_min > self.limit or end > ctx.end_min:
            return None
        if opening_status(c, start, end) == "closed":
            return None
        if self.crowd_avoid and likely_busy(c, start, end):
            return None
        walking_stop = is_walking_stop(c)
        if self.sunset is not None and walking_stop and is_outdoor(c) and end > self.sunset:
            return None
        walk = (node.walk if node else 0) + (leg.minutes if leg and leg.mode == "walk" else 0)
        walk += dur if walking_stop else 0
        if walk > pacing.max_walk_minutes:
            return None
        long_legs = (node.long_legs if node else 0) + (1 if leg and leg.minutes > pacing.long_leg_minutes else 0)
        if long_legs > pacing.max_long_legs:
            return None
        cost = (node.cost_max if node else 0) + c.cost.max + (leg.fare.max if leg else 0)
        if self.budget is not None and worst_case(cost, 0) > self.budget:
            return None
        return _Node(
            idx=(node.idx if node else ()) + (j,),
            durs=(node.durs if node else ()) + (dur,),
            starts=(node.starts if node else ()) + (start,),
            end=end,
            cost_max=cost,
            walk=walk,
            long_legs=long_legs,
            seated=(node.seated if node else 0) + (1 if c.seated else 0),
            leg_minutes=(node.leg_minutes if node else 0) + (leg.minutes if leg else 0),
            covered=(node.covered if node else frozenset()) | self.pool_covered[j],
            includes=(node.includes if node else frozenset()) | self.pool_includes[j],
            base=(node.base if node else 0.0) + self.pool_scores[j],
            meal_hit=(node.meal_hit if node else False) or self._meal_hit(c, start, end),
            cats=(node.cats if node else ()) + (c.category,),
        )

    def _simulate(self, idx: Sequence[int], durs: Sequence[int]) -> _Node | None:
        node: _Node | None = None
        for j, d in zip(idx, durs, strict=True):
            node = self._extend(node, j, d)
            if node is None:
                return None
        return node

    # -- ranking --------------------------------------------------------------------------------------

    def _unmet(self, n: _Node) -> int:
        return (len(self.includes) - len(n.includes)) + max(0, self.pacing.min_seated_stops - n.seated)

    def _quality(self, n: _Node) -> float:
        s = n.base + 6.0 * len(n.covered) + 6.0 * len(n.includes) - 0.08 * n.leg_minutes
        if self.meal_targets and n.meal_hit:
            s += 3.0
        used = (n.end - self.ctx.start_min) / max(1, self.limit)
        s += 4.0 * min(1.0, used / 0.6)
        s -= 2.5 * (len(n.cats) - len(set(n.cats)))
        s -= 5.0 * max(0, self.pacing.min_seated_stops - n.seated)
        return s

    def _rank_key(self, n: _Node, objective: Objective) -> tuple[float, ...]:
        if objective == "cheapest":
            return (self._unmet(n), worst_case(n.cost_max, 0), -self._quality(n))
        if objective == "shortest":
            return (self._unmet(n), n.end - self.ctx.start_min, -self._quality(n))
        return (-self._quality(n),)

    # -- search ---------------------------------------------------------------------------------------

    def _beam(self, objective: Objective) -> list[_Node]:
        complete: dict[tuple[int, ...], _Node] = {}
        frontier: list[_Node | None] = [None]
        for _depth in range(self.pacing.max_stops):
            children: dict[tuple[frozenset[int], int], _Node] = {}
            for node in frontier:
                for j in range(len(self.pool)):
                    for dur in self._duration_options(self.pool[j], objective):
                        child = self._extend(node, j, dur)
                        if child is None:
                            continue
                        key = (frozenset(child.idx), j)
                        prev = children.get(key)
                        if prev is None or self._rank_key(child, objective) < self._rank_key(prev, objective):
                            children[key] = child
                        break
            if not children:
                break
            ranked = sorted(children.values(), key=lambda n: self._rank_key(n, objective))
            for n in ranked[: BEAM_WIDTH * 3]:
                if self._unmet(n) == 0:
                    complete.setdefault(n.idx, n)
            frontier = list(ranked[:BEAM_WIDTH])
        return sorted(complete.values(), key=lambda n: self._rank_key(n, objective))

    def _stretch(self, node: _Node) -> _Node:
        durs = list(node.durs)
        order = sorted(range(len(durs)), key=lambda k: (-self.pool_scores[node.idx[k]], k))
        for k in order:
            c = self.pool[node.idx[k]]
            if c.kind == "event":
                continue
            _, hi = duration_bounds(c)
            for target in (hi, (durs[k] + hi) // 2, durs[k] + (hi - durs[k]) // 4):
                if target <= durs[k]:
                    continue
                trial = list(durs)
                trial[k] = target
                sim = self._simulate(node.idx, trial)
                if sim is not None:
                    durs, node = trial, sim
                    break
        return node

    def search(self, objective: Objective = "best", max_results: int = 1) -> list[SearchResult]:
        if not self.pool:
            return []
        finalists = self._beam(objective)
        if objective == "best":
            stretched = [self._stretch(n) for n in finalists[:FINALISTS]]
            finalists = sorted(stretched, key=lambda n: self._rank_key(n, objective))
        results: list[SearchResult] = []
        for node in finalists[:MAX_VALIDATIONS]:
            draft = self.to_draft(node)
            report = validate_plan(draft, self.ledger, self.ctx, self.validation_prefs, self.constraints, self.travel)
            if report.ok:
                results.append(SearchResult(draft, report))
                if len(results) >= max_results:
                    break
        return results

    # -- draft text -----------------------------------------------------------------------------------

    def _plan_b(self, c: Candidate, start: int, end: int, used: set[str]) -> str | None:
        options = []
        for e in self.eligible:
            if e.id in used or e.kind == "event" or e.indoor is not True:
                continue
            if plan_b_problems(e, c, self.constraints, self.ctx):
                continue
            closed = opening_status(e, start, end) == "closed"
            busy = likely_busy(e, start, end)
            km = haversine_km(c.lat, c.lng, e.lat, e.lng)
            options.append((closed, busy, round(km, 1), -self.scores.get(e.id, 0.0), e.id))
        return min(options)[-1] if options else None

    def _why(self, c: Candidate, k: int, start: int, end: int) -> str:
        energy = self.prefs.energy
        label = c.category.replace("_", " ")
        if c.kind == "event":
            lead = "A live event happening on the day"
        elif energy == Energy.LOW and c.seated:
            lead = "Seated and calm for a low-energy start" if k == 0 else "A seated, unhurried stop to recharge"
        elif energy == Energy.LOW:
            lead = "Gentle and low effort" if c.effort == "low" else "An easy-going change of scene"
        elif energy == Energy.HIGH:
            lead = f"Keeps the day moving with a lively {label}"
        else:
            lead = f"A well-rated {label}" if (c.rating or 0) >= 4.3 else f"A pleasant {label}"
        parts = [lead]
        if is_meal_stop(c) and any(start < w.end_min and w.start_min < end for w in self.meal_targets
                                   if w.name != "snack"):
            parts.append("a proper meal at a sensible time")
        for con in self.constraints:
            if con.kind == "diet" and is_food_stop(c):
                parts.append(f"{DIET_LABEL.get(con.value, con.value)} menu confirmed")
            elif con.kind == "access":
                parts.append("step-free access confirmed")
            elif con.kind == "crowd" and con.value == "avoid":
                parts.append("usually calm at this time")
        if self.ctx.weather.is_rainy and c.indoor is True:
            parts.append("indoors, so the rain won't matter")
        labels = [lab for i in self.inputs.interests if i in covered_interests(c, [i])
                  if (lab := safe_interest_label(i))]
        if labels:
            parts.append("fits your love of " + _join(labels[:2]))
        text = parts[0]
        for p in parts[1:]:
            if len(text) + len(p) + 3 > 290:
                break
            text += "; " + p
        return text + "."

    def _tradeoff(self, c: Candidate, leg: Leg | None, has_plan_b: bool) -> str | None:
        opts: list[str] = []
        if c.kind == "place" and c.hours.status != "known":
            opts.append("Opening hours aren't listed, so check before you go.")
        if c.kind == "event":
            opts.append("Event details come from the listing, so check before you go.")
        if any(con.key == "alcohol:avoid" for con in self.constraints) and c.serves_alcohol:
            opts.append("It also serves alcohol, though that isn't its focus.")
        if self.ctx.weather.is_rainy and is_outdoor(c):
            opts.append("Rain is likely; switch to the indoor Plan B if it pours." if has_plan_b
                        else "Rain is likely and there's no indoor backup nearby.")
        if leg is not None and leg.minutes > self.pacing.long_leg_minutes:
            opts.append("Needs a longer ride to get here.")
        if self.inputs.interests and not covered_interests(c, self.inputs.interests):
            opts.append("Not one of your stated interests, but it suits the pace and the budget.")
        if self.budget and c.cost.max * 10 >= self.budget * 4:
            opts.append("One of the pricier picks, though it still fits the budget.")
        text = " ".join(opts[:2])
        return text[:240] or None

    def _summary(self, node: _Node) -> str:
        pace = {Energy.LOW: "An easy-paced", Energy.MEDIUM: "A relaxed", Energy.HIGH: "A full"}[self.prefs.energy]
        labels = [lab for i in self.inputs.interests if i in node.covered if (lab := safe_interest_label(i))]
        text = f"{pace} plan" + (f" around {_join(labels[:4])}" if labels else "") + "."
        if self.ctx.weather.is_rainy:
            text += " Rain is likely, so it leans indoors."
        diets = [DIET_LABEL.get(c.value, c.value) for c in self.constraints if c.kind == "diet"]
        if diets:
            text += f" Every food stop has confirmed {diets[0]} options."
        if any(c.kind == "access" for c in self.constraints):
            text += " Every stop has confirmed wheelchair access."
        if self.crowd_avoid:
            text += " Stops are timed for calmer hours (a heuristic, not live data)."
        return text[:400]

    def _omissions(self, used: set[str]) -> list[Omission]:
        scored = []
        for cid, reasons in self.excluded.items():
            c = self.ledger[cid]
            cov = len(covered_interests(c, self.inputs.interests))
            if cov == 0:
                continue
            scored.append((-cov, -(c.rating or 0.0), cid, reasons[0]))
        out = []
        for _cov, _rating, cid, reason in sorted(scored)[:3]:
            if cid in used:
                continue
            out.append(Omission(ref=cid[:120], reason=OMIT_REASONS.get(reason, "Left out to keep the plan valid.")))
        return out

    def to_draft(self, node: _Node) -> PlanDraft:
        used = {self.pool[j].id for j in node.idx}
        stops: list[DraftStop] = []
        plan_b_used: set[str] = set(used)
        for k, (j, dur, start) in enumerate(zip(node.idx, node.durs, node.starts, strict=True)):
            c = self.pool[j]
            end = start + dur
            leg = self._leg(node.idx[k - 1], j) if k > 0 else None
            plan_b = None
            if self.ctx.weather.is_rainy and is_outdoor(c):
                plan_b = self._plan_b(c, start, end, plan_b_used)
                if plan_b:
                    plan_b_used.add(plan_b)
            why = self._why(c, k, start, end)
            tradeoff = self._tradeoff(c, leg, plan_b is not None)
            if numeric_claims(why):
                why = "A good fit for the pace and the budget."
            if tradeoff and numeric_claims(tradeoff):
                tradeoff = None
            stops.append(DraftStop(candidate_id=c.id, duration_min=dur, why=why, tradeoff=tradeoff,
                                   plan_b_candidate_id=plan_b))
        return PlanDraft(stops=stops, omitted=self._omissions(used), summary_note=self._summary(node))


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def search_plans(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                 constraints: list[Constraint], travel: TravelEstimator, *, objective: Objective = "best",
                 ignore_budget: bool = False) -> SearchResult | None:
    """Best (or cheapest/shortest) valid plan with its validation report, or None."""
    planner = Planner(candidates, ctx, prefs, constraints, travel, ignore_budget=ignore_budget)
    results = planner.search(objective)
    return results[0] if results else None


def plan_deterministic(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                       constraints: list[Constraint], travel: TravelEstimator) -> PlanDraft | None:
    """Best valid draft or None. Every returned draft passes validate_plan with no errors.
    why/tradeoff texts are templates with no numbers (PROSE_NUMERIC_CLAIM must not fire).

    ``constraints`` are enforced together with ``prefs.hard_constraints`` (validate_plan merges them too)."""
    result = search_plans(candidates, ctx, prefs, constraints, travel)
    return result.draft if result else None


def ledger_of(candidates: Sequence[Candidate]) -> Mapping[str, Candidate]:
    out: dict[str, Candidate] = {}
    for c in candidates:
        out.setdefault(c.id, c)
    return out
