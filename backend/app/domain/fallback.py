"""Deterministic planner (no LLM): prefilter, score, beam search, validate. OWNER: domain agent.

Search: candidates are prefiltered (constraints with positive evidence, city radius, sources, currency, event
date, opening hours inside the window, budget, programme venues without verified hours), scored
(app.domain.scoring), and a beam search (width ``BEAM_WIDTH``) builds ordered sequences up to the pacing
profile's max stops. Each extension is simulated with the same arithmetic as the validator (legs from the
TravelEstimator, buffers, opening hours, crowds, daylight, walking, long legs, budget with contingency, time)
plus planner-only realism rules:

* a full sit-down meal is only scheduled when it starts inside breakfast/lunch/dinner and spends at least half
  of the visit there (unless the user requires a sit-down meal), and at most once per meal window;
* waiting before an event is capped at ``MAX_EVENT_WAIT_MIN`` and idle time never counts as time used;
* no two stops of the same category back to back.

Complete sequences (must_include met, enough seated stops) are ranked by quality (interest coverage,
candidate scores, meal timing, time actually used, travel), durations start at the category default and are
stretched moderately while time remains, an indoor Plan B (open at that time, not another stop, within budget
if taken) is added for outdoor stops when rain is likely, and finalists are checked with ``validate_plan``
until one passes.

The same search runs in "cheapest" and "shortest" modes for app.domain.infeasible.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.domain.constraints import merge_constraints
from app.domain.costs import worst_case
from app.domain.evidence import (
    PROGRAMME_CATEGORIES,
    calm_at,
    candidate_allowed,
    category_info,
    event_window,
    has_approx_location,
    is_food_stop,
    is_full_meal,
    is_meal_stop,
    is_outdoor,
    is_snack_stop,
    is_walking_stop,
    likely_busy,
    main_meal_window,
    meal_slot,
    open_overlap_minutes,
    opening_status,
    satisfies_include,
)
from app.domain.fill import fill_window
from app.domain.realworld import admission_estimate_unverified, prefers_short_travel
from app.domain.schedule import plan_currency
from app.domain.schemas import (
    Candidate,
    Constraint,
    DraftStop,
    Energy,
    Leg,
    MealWindow,
    Omission,
    PacingProfile,
    PlanDraft,
    Preferences,
    ValidationReport,
    WorldContext,
)
from app.domain.scoring import (
    build_inputs,
    candidate_tags,
    covered_interests,
    safe_interest_label,
    score_candidate,
)
from app.domain.sun import effective_sunset
from app.domain.travel import coords_valid, haversine_km
from app.domain.validator import (
    CITY_RADIUS_KM,
    IDLE_WAIT_MIN,
    duration_bounds,
    numeric_claims,
    plan_b_problems,
    plan_b_timing_problems,
    seated_rule_waived,
    source_problems,
    validate_plan,
    worst_case_with_plan_b,
)
from app.providers.base import TravelEstimator

BEAM_WIDTH = 30
POOL_LIMIT = 40
POOL_PER_NEED = 5
FINALISTS = 25
MAX_VALIDATIONS = 12
MAX_EVENT_WAIT_MIN = 30
MEAL_STRETCH_MAX_MIN = 120  # how far earlier stops may be lengthened so a meal starts when its window opens
# Stretch each stop from its default toward its maximum by this share first; the rest only if the plan would
# otherwise use less than MIN_USE of the window.
STRETCH_SHARE = {Energy.LOW: 0.34, Energy.MEDIUM: 0.5, Energy.HIGH: 0.5}
MIN_USE = 0.6
# Stated order from the parser, e.g. "order: brunch > art" (soft preference; a strong one in ranking).
ORDER_PREFIX = "order: "
MORNING_ITEMS = frozenset({"brunch", "breakfast"})
MEAL_ITEMS = frozenset({"brunch", "breakfast", "lunch", "dinner"})
MORNING_MEAL_WINDOW = (7 * 60 + 30, 12 * 60 + 30)  # brunch/breakfast stops must start in here
ORDER_MATCH_BONUS = 8.0
ORDER_FIRST_MISS_PENALTY = 6.0
ORDER_SKIP_PENALTY = 8.0
# Places with unknown hours are only planned inside ordinary daytime hours, except night-time categories.
UNKNOWN_HOURS_DAY = (8 * 60, 22 * 60)
NIGHT_GROUPS = frozenset({"drinks", "entertainment"})

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
    "currency": "Left out because it's priced in a different currency from your budget.",
    "programme": "Left out because it needs a scheduled show and no listed times were found.",
}


# ---------------------------------------------------------------------------------------------------
# Stated order
# ---------------------------------------------------------------------------------------------------


def order_items(prefs: Preferences) -> list[str]:
    """Items of the parser's "order: a > b" soft preference (lowercase), or [] when no order was stated."""
    for pref in prefs.soft_preferences:
        if pref.lower().startswith(ORDER_PREFIX):
            return [item.strip().lower() for item in pref[len(ORDER_PREFIX):].split(">") if item.strip()]
    return []


def order_match_level(c: Candidate, item: str) -> int:
    """Time-independent match of a stop to an order item: 2 strong, 1 weak, 0 none. Brunch/breakfast also need
    the stop to start in the morning meal window (checked by ``order_match_at``)."""
    tags = candidate_tags(c)
    if item in MORNING_ITEMS:
        if c.category not in {"cafe", "restaurant"}:
            return 0
        return 2 if tags & MORNING_ITEMS else 1
    if item in {"lunch", "dinner"}:
        return 2 if is_full_meal(c) else (1 if is_meal_stop(c) else 0)
    return 2 if covered_interests(c, [item]) else 0


def order_match_at(level: int, item: str, start_min: int) -> int:
    if level and item in MORNING_ITEMS and not MORNING_MEAL_WINDOW[0] <= start_min < MORNING_MEAL_WINDOW[1]:
        return 0
    return level


# ---------------------------------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------------------------------


def prefilter(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
              constraints: list[Constraint], *, budget: int | None) -> tuple[list[Candidate], dict[str, list[str]]]:
    """(eligible candidates, {excluded id: [reason keys]}). Reason keys index OMIT_REASONS; constraint
    exclusions use the constraint kind."""
    currency = plan_currency(ctx, prefs)
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
        if c.cost.currency != currency:
            reasons.append("currency")
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
            if c.category in PROGRAMME_CATEGORIES and c.hours.status != "known":
                reasons.append("programme")  # a concert hall or theatre is only a plan when something is on
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
    idle: int
    covered: frozenset[str]
    includes: frozenset[str]
    base: float
    meal_credit: frozenset[str]  # meal windows (incl. snack) served well by a stop
    full_meals: tuple[str, ...]  # main meal window of each full meal ("" when misaligned)
    cats: tuple[str, ...]
    order_next: int = 0  # index of the next stated-order item still to be met
    order_score: float = 0.0
    early_meals: int = 0  # sit-down meals starting shortly before their window (an early lunch/dinner)
    long_waits: int = 0  # waits over IDLE_WAIT_MIN before a stop (validator warns IDLE_WAIT)
    outdoor: int = 0  # outdoor stops (penalised when rain is likely)


@dataclass
class SearchResult:
    draft: PlanDraft
    report: ValidationReport


class Planner:
    """Reusable search over one candidate set. ``ignore_budget`` / relaxed contexts are used by infeasible."""

    def __init__(self, candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                 constraints: list[Constraint], travel: TravelEstimator, *, ignore_budget: bool = False,
                 pacing: PacingProfile | None = None, meal_timing: bool = True) -> None:
        if pacing is not None:
            ctx = ctx.model_copy(update={"pacing": pacing})
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
        self.min_seated = 0 if seated_rule_waived(self.constraints, self.ledger, ctx) else self.pacing.min_seated_stops
        keys = {c.key for c in self.constraints}
        self.crowd_avoid = "crowd:avoid" in keys
        self.includes = tuple(c.value for c in self.constraints if c.kind == "must_include")
        self.meal_required = "sit_down_meal" in self.includes
        self.meal_timing = meal_timing  # False only in infeasible's "is meal timing what binds?" test
        self.order = order_items(prefs)
        self.sunset = effective_sunset(ctx)
        self.eligible, self.excluded = prefilter(list(self.ledger.values()), ctx, prefs, self.constraints,
                                                 budget=self.budget)
        self.inputs = build_inputs(self.eligible, ctx, prefs, self.constraints)
        self.scores = {c.id: score_candidate(c, self.inputs) for c in self.eligible}
        self.pool = self._select_pool()
        self.pool_scores = [self.scores[c.id] for c in self.pool]
        self.pool_covered = [frozenset(covered_interests(c, self.inputs.interests)) for c in self.pool]
        self.pool_includes = [frozenset(v for v in self.includes if satisfies_include(c, v)) for c in self.pool]
        self.pool_order = [[order_match_level(c, item) for item in self.order] for c in self.pool]
        self.meal_targets: tuple[MealWindow, ...] = tuple(
            w for w in ctx.meal_windows
            if min(w.end_min, ctx.end_min) - max(w.start_min, ctx.start_min) >= 30
        )
        # a long window that really spans lunch or dinner should include that meal
        self.main_meal_weight: dict[str, float] = {
            w.name: 7.0 if min(w.end_min, ctx.end_min) - max(w.start_min, ctx.start_min) >= 60 else 3.0
            for w in self.meal_targets if w.name != "snack"
        }
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
        add_top(is_snack_stop)
        if self.min_seated:
            add_top(lambda c: c.seated)
        for interest in self.inputs.interests:
            add_top(covers(interest))

        def matches_order(item: str) -> Callable[[Candidate], bool]:
            return lambda c: order_match_level(c, item) > 0

        for item in order_items(self.prefs):
            add_top(matches_order(item))
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

    def _meal_credit(self, c: Candidate, start: int, end: int) -> str | None:
        """The target meal window this stop serves well, if any."""
        if is_full_meal(c):
            slot = meal_slot(start, end)
            return slot[0].name if slot is not None and slot[0] in self.meal_targets else None
        if is_snack_stop(c):
            for w in self.meal_targets:
                if w.name == "snack":
                    inside = min(end, w.end_min) - max(start, w.start_min)
                    if inside * 2 >= end - start:
                        return w.name
        return None

    def _extend(self, node: _Node | None, j: int, dur: int) -> _Node | None:
        ctx, pacing = self.ctx, self.pacing
        c = self.pool[j]
        leg: Leg | None = None
        if node is None:
            arrival = ctx.start_min
        else:
            if j in node.idx or len(node.idx) >= pacing.max_stops:
                return None
            if node.cats[-1] == c.category:
                return None  # no back-to-back stops of the same kind (coffee then more coffee)
            leg = self._leg(node.idx[-1], j)
            if leg is None:
                return None
            arrival = node.end + leg.minutes_max + pacing.buffer_minutes
        wait = 0
        if c.kind == "event" and c.event_start_min is not None:
            if arrival > c.event_start_min:
                return None
            wait = c.event_start_min - arrival
            if wait > MAX_EVENT_WAIT_MIN:
                return None
            start = c.event_start_min
        else:
            start = arrival
        if is_outdoor(c) and is_walking_stop(c) and ctx.weather.sunrise_min is not None:
            start = max(start, ctx.weather.sunrise_min)
        end = start + dur
        if end - ctx.start_min > self.limit or end > ctx.end_min:
            return None
        status = opening_status(c, start, end)
        if status == "closed":
            return None
        if status == "unknown" and c.kind == "place" and not self._plausible_unknown_hours(c, start, end):
            return None
        if self.crowd_avoid and likely_busy(c, start, end):
            return None
        walking_stop = is_walking_stop(c)
        if walking_stop and is_outdoor(c):
            if self.sunset is not None and end > self.sunset:
                return None
            if ctx.weather.sunrise_min is not None and start < ctx.weather.sunrise_min:
                return None
        full_meals = node.full_meals if node else ()
        early_meals = node.early_meals if node else 0
        if is_full_meal(c):
            slot = meal_slot(start, end)
            brunch = any(order_match_at(self.pool_order[j][i], item, start)
                         for i, item in enumerate(self.order) if item in MORNING_ITEMS)
            if slot is None and not brunch and not self.meal_required and self.meal_timing:
                return None  # no thali at three in the afternoon
            name = slot[0].name if slot else ("brunch" if brunch else "")
            if name and name in full_meals:
                return None  # one sit-down meal per meal window
            full_meals = (*full_meals, name)
            if slot is not None and slot[1] > 0:
                early_meals += 1
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
        order_next, order_score = self._order_progress(node, j, start)
        credit = self._meal_credit(c, start, end)
        meal_credit = node.meal_credit if node else frozenset()
        if credit:
            meal_credit = meal_credit | {credit}
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
            idle=(node.idle if node else 0) + wait,
            covered=(node.covered if node else frozenset()) | self.pool_covered[j],
            includes=(node.includes if node else frozenset()) | self.pool_includes[j],
            base=(node.base if node else 0.0) + self.pool_scores[j],
            meal_credit=meal_credit,
            full_meals=full_meals,
            cats=(node.cats if node else ()) + (c.category,),
            order_next=order_next,
            order_score=order_score,
            early_meals=early_meals,
            long_waits=(node.long_waits if node else 0) + (1 if wait > IDLE_WAIT_MIN else 0),
            outdoor=(node.outdoor if node else 0) + (1 if is_outdoor(c) else 0),
        )

    def _order_progress(self, node: _Node | None, j: int, start: int) -> tuple[int, float]:
        """Advance the stated order: reward the next item's stop, penalise skipping ahead or a wrong first stop."""
        nxt = node.order_next if node else 0
        score = node.order_score if node else 0.0
        if not self.order:
            return nxt, score
        levels = [order_match_at(self.pool_order[j][i], item, start) for i, item in enumerate(self.order)]
        if nxt < len(self.order) and levels[nxt]:
            score += ORDER_MATCH_BONUS + (1.5 if levels[nxt] == 2 else 0.0)
            nxt += 1
        else:
            later = [i for i in range(nxt + 1, len(self.order)) if levels[i]]
            if later:
                score -= ORDER_SKIP_PENALTY * (later[0] - nxt)
                nxt = later[0] + 1
            if node is None:
                score -= ORDER_FIRST_MISS_PENALTY
        return nxt, score

    def _extend_into_meal(self, node: _Node | None, j: int, dur: int) -> _Node | None:
        """Like _extend, but if a sit-down meal would arrive before a main meal window that the plan spans,
        stretch earlier stops (within their maxima) so the meal starts when the window opens. An early meal
        (allowed up to MEAL_EARLY_MIN before the window) is kept only when no in-window variant exists."""
        child = self._extend(node, j, dur)
        c = self.pool[j]
        if node is None or not is_full_meal(c) or j in node.idx:
            return child
        if child is not None and child.early_meals == node.early_meals:
            return child  # already in its window
        leg = self._leg(node.idx[-1], j)
        if leg is None:
            return child
        arrival = node.end + leg.minutes_max + self.pacing.buffer_minutes
        for w in self.meal_targets:
            if w.name == "snack" or not (arrival < w.start_min <= arrival + MEAL_STRETCH_MAX_MIN):
                continue
            need = w.start_min - arrival
            durs = list(node.durs)
            for k in reversed(range(len(durs))):
                ck = self.pool[node.idx[k]]
                if ck.kind == "event":
                    continue
                take = min(duration_bounds(ck)[1] - durs[k], need)
                durs[k] += max(0, take)
                need -= max(0, take)
                if need == 0:
                    break
            if need:
                continue
            prefix = self._simulate(node.idx, durs)
            if prefix is not None:
                aligned = self._extend(prefix, j, dur)
                if aligned is not None:
                    return aligned
        return child

    @staticmethod
    def _plausible_unknown_hours(c: Candidate, start: int, end: int) -> bool:
        """Unknown hours: don't send anyone to a museum at midnight. Night-time categories are exempt."""
        info = category_info(c)
        if info is not None and info.group in NIGHT_GROUPS:
            return True
        return UNKNOWN_HOURS_DAY[0] <= start and end <= UNKNOWN_HOURS_DAY[1]

    def _simulate(self, idx: Sequence[int], durs: Sequence[int]) -> _Node | None:
        node: _Node | None = None
        for j, d in zip(idx, durs, strict=True):
            node = self._extend(node, j, d)
            if node is None:
                return None
        return node

    # -- ranking --------------------------------------------------------------------------------------

    def _unmet(self, n: _Node) -> int:
        return (len(self.includes) - len(n.includes)) + max(0, self.min_seated - n.seated)

    def _used(self, n: _Node) -> float:
        """Share of the window actually spent at stops or travelling (idle waits don't count)."""
        return (n.end - self.ctx.start_min - n.idle) / max(1, self.limit)

    def _quality(self, n: _Node) -> float:
        travel_penalty = 0.4 if prefers_short_travel(self.prefs) else 0.08
        s = n.base + 6.0 * len(n.covered) + 6.0 * len(n.includes) - travel_penalty * n.leg_minutes - 0.03 * n.idle
        for name in n.meal_credit:
            s += 1.5 if name == "snack" else self.main_meal_weight.get(name, 3.0)
        s -= 2.0 * n.early_meals  # an early lunch/dinner is fine, an on-time one is better
        s -= 4.0 * n.long_waits  # avoid standing around before an event when another plan exists
        s -= 4.0 * sum(1 for name in n.full_meals if not name)  # a required meal at an odd time
        used = self._used(n)
        s += 4.0 * min(1.0, used / MIN_USE) + 3.0 * min(1.0, max(0.0, used - MIN_USE) / 0.25)
        if self.ctx.weather.is_rainy:
            s -= 4.0 * n.outdoor  # all-indoor when rain is likely and indoor options exist
        s -= 2.5 * (len(n.cats) - len(set(n.cats)))
        s -= 5.0 * max(0, self.min_seated - n.seated)
        s += n.order_score
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
            children: dict[tuple[frozenset[int], int, int], _Node] = {}
            for node in frontier:
                for j in range(len(self.pool)):
                    for dur in self._duration_options(self.pool[j], objective):
                        child = self._extend_into_meal(node, j, dur)
                        if child is None:
                            continue
                        key = (frozenset(child.idx), j, child.end)
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

    def _stretch_pass(self, node: _Node, share: float) -> _Node:
        durs = list(node.durs)
        order = sorted(range(len(durs)), key=lambda k: (-self.pool_scores[node.idx[k]], k))
        for k in order:
            c = self.pool[node.idx[k]]
            if c.kind == "event":
                continue
            _, hi = duration_bounds(c)
            default = min(max(c.duration_range[1], durs[k]), hi)
            cap = default + round((hi - default) * share)
            for target in (cap, (durs[k] + cap) // 2):
                if target <= durs[k]:
                    continue
                trial = list(durs)
                trial[k] = target
                sim = self._simulate(node.idx, trial)
                if sim is not None:
                    durs, node = trial, sim
                    break
        return node

    def _stretch(self, node: _Node) -> _Node:
        """Moderate stretch toward each stop's maximum; go further only if the plan would feel empty."""
        node = self._stretch_pass(node, STRETCH_SHARE[self.prefs.energy])
        if self._used(node) < MIN_USE:
            node = self._stretch_pass(node, 1.0)
        return node

    def search(self, objective: Objective = "best", max_results: int = 1) -> list[SearchResult]:
        if not self.pool:
            return []
        finalists = self._beam(objective)
        if objective == "best":
            stretched = [self._stretch(n) for n in finalists[:FINALISTS]]
            finalists = sorted(stretched, key=lambda n: self._rank_key(n, objective))
        drafts = [self.to_draft(node) for node in finalists[: MAX_VALIDATIONS * 2]]
        if self.ctx.weather.is_rainy:  # plans whose outdoor stops all have an indoor Plan B first
            drafts.sort(key=lambda d: self._missing_plan_b(d))
        results: list[SearchResult] = []
        for draft in drafts[:MAX_VALIDATIONS]:
            report = validate_plan(draft, self.ledger, self.ctx, self.validation_prefs, self.constraints, self.travel)
            if any(v.code == "PLAN_B_INVALID" for v in report.errors):
                shorter = draft.model_copy(update={"stops": [
                    s.model_copy(update={"duration_min": duration_bounds(self.ledger[s.candidate_id])[0]})
                    if self.ledger[s.candidate_id].kind != "event" else s for s in draft.stops]})
                repaired = validate_plan(shorter, self.ledger, self.ctx, self.validation_prefs, self.constraints, self.travel)
                if repaired.ok:
                    draft, report = shorter, repaired
                elif any(c.key in {"must_include:outdoor_walk", "setting:outdoors_only"} for c in self.constraints):
                    # An indoor substitution cannot fulfill an explicitly required outdoor activity.
                    draft = draft.model_copy(update={"stops": [s.model_copy(update={
                        "plan_b_candidate_id": None,
                        "tradeoff": "Rain may make this outdoor stop unsuitable; no compatible indoor alternative is verified."
                    }) if s.plan_b_candidate_id else s for s in draft.stops]})
                    report = validate_plan(draft, self.ledger, self.ctx, self.validation_prefs, self.constraints, self.travel)
            if report.ok and objective == "best" and any(v.code == "UNDERFILLED" for v in report.violations):
                filled = fill_window(draft, self.ledger, self.ctx, self.validation_prefs, self.constraints,
                                     self.travel)  # same bar the validator applies to agent plans
                if filled is not draft:
                    draft = filled
                    report = validate_plan(draft, self.ledger, self.ctx, self.validation_prefs, self.constraints,
                                           self.travel)
            if report.ok:
                results.append(SearchResult(draft, report))
                if len(results) >= max_results:
                    break
        if not results and objective == "best":
            return self.search("shortest", max_results)
        return results

    def _missing_plan_b(self, draft: PlanDraft) -> int:
        return sum(1 for s in draft.stops if is_outdoor(self.ledger[s.candidate_id]) and s.plan_b_candidate_id is None)

    # -- Plan B ---------------------------------------------------------------------------------------

    def _plan_b(self, c: Candidate, start: int, end: int, stop_ids: set[str], taken: set[str],
                stop_costs: list[int], plan_b_costs: list[int | None], k: int, legs_cost_max: int) -> str | None:
        options = []
        for e in self.eligible:
            if e.id in taken or e.kind == "event" or e.indoor is not True:
                continue
            if plan_b_problems(e, c, self.constraints, self.ctx):
                continue
            errors, warnings = plan_b_timing_problems(e, start, end, stop_ids)
            if errors:
                continue
            if self.budget is not None:
                trial = list(plan_b_costs)
                trial[k] = e.cost.max
                if worst_case_with_plan_b(stop_costs, trial, legs_cost_max) > self.budget:
                    continue
            busy = likely_busy(e, start, end)
            km = haversine_km(c.lat, c.lng, e.lat, e.lng)
            options.append((has_approx_location(e), bool(warnings), busy, round(km, 1), -self.scores.get(e.id, 0.0),
                            e.id))
        return min(options)[-1] if options else None

    # -- draft text -----------------------------------------------------------------------------------

    def _lead(self, c: Candidate, k: int, used_leads: set[str]) -> str:
        """An opening phrase; each template is used at most once per plan so stops don't all read alike."""
        energy = self.prefs.energy
        label = c.category.replace("_", " ")
        good = (c.rating or 0) >= 4.3
        templates: list[tuple[str, str]]
        if c.kind == "event":
            templates = [("event-live", "A live event happening on the day"),
                         ("event-today", "Something on only today")]
        elif energy == Energy.LOW and c.seated:
            templates = ([("low-start", "Seated and calm for a low-energy start")] if k == 0 else []) + [
                ("low-seat", "A seated, unhurried stop to recharge"),
                ("low-sit", "A comfortable place to sit back for a while"),
                ("low-easy", f"An easy, seated {label}")]
        elif energy == Energy.LOW:
            templates = [("low-gentle", "Gentle and low effort"), ("low-scene", "An easy-going change of scene"),
                         ("low-light", f"A light-touch {label} visit")]
        elif energy == Energy.HIGH:
            templates = [("high-moving", f"Keeps the day moving with a {label}"),
                         ("high-variety", f"Adds variety with a {label}"),
                         ("high-pace", f"A {'well-rated' if good else 'fun'} {label} to keep the pace up"),
                         ("high-scene", "Another change of scene")]
        else:
            templates = [("mid-rated", f"A {'well-rated' if good else 'pleasant'} {label}"),
                         ("mid-relaxed", f"A relaxed {label} stop"),
                         ("mid-value", f"A good-value {label}" if c.cost.max == 0 else f"A worthwhile {label}"),
                         ("mid-nice", f"A nice {label} to break up the day")]
        for key, text in templates:
            if key not in used_leads:
                used_leads.add(key)
                return text
        return templates[k % len(templates)][1]

    def _why(self, c: Candidate, k: int, start: int, end: int, used_leads: set[str],
             order_item: str | None = None) -> str:
        parts = [self._lead(c, k, used_leads)]
        window = main_meal_window(start, end) if is_full_meal(c) else None
        if order_item and (label := safe_interest_label(order_item)):
            parts.append(f"{label} {'first' if k == 0 else 'next'}, as you asked")
        slot = meal_slot(start, end) if is_full_meal(c) else None
        if window is not None:
            parts.append(f"a proper {window.name} at a sensible time")
        elif slot is not None:
            parts.append(f"an early {slot[0].name}")
        elif is_snack_stop(c) and self._meal_credit(c, start, end) == "snack":
            parts.append("a well-timed afternoon bite")
        for con in self.constraints:
            if con.kind == "diet" and is_food_stop(c):
                parts.append(f"{DIET_LABEL.get(con.value, con.value)} menu confirmed")
            elif con.kind == "access":
                parts.append("the source reports a wheelchair-accessible entrance")
        if calm_at(c, start, end):
            parts.append("usually calm at this time")
        if self.ctx.weather.is_rainy and c.indoor is True:
            parts.append("indoors, so the rain won't matter")
        mood_hits = sorted(self.inputs.mood_tags & {t.lower() for t in c.tags})
        if mood_hits and (lab := safe_interest_label(mood_hits[0])):
            parts.append(f"suits a {lab} mood" if lab != "romantic" else "a romantic touch")
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

    def _tradeoff(self, c: Candidate, leg: Leg | None, has_plan_b: bool, priciest: bool) -> str | None:
        opts: list[str] = []
        if admission_estimate_unverified(c):
            opts.append("Admission price is unverified; confirm the ticket before leaving.")
        if "admission:general_free" in c.tags:
            opts.append("General admission is free; optional paid exhibitions are not included.")
        if self.inputs.quiet and {"music", "live music", "nightlife"} & candidate_tags(c):
            opts.append("Live music is never silent, but this is one of the calmer rooms, so it stays fairly quiet."
                        if c.popularity == "low" else
                        "Livelier than the quiet you asked for, but it covers your interest in music.")
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
        if priciest:
            opts.append("The priciest stop in the plan, though it still fits the budget.")
        text = " ".join(opts[:2])
        return text[:240] or None

    def _summary(self, node: _Node) -> str:
        pace = {Energy.LOW: "An easy-paced", Energy.MEDIUM: "A relaxed", Energy.HIGH: "A full"}[self.prefs.energy]
        labels = [lab for i in self.inputs.interests if i in node.covered if (lab := safe_interest_label(i))]
        text = f"{pace} plan" + (f" around {_join(labels[:4])}" if labels else "") + "."
        stops = [self.pool[j] for j in node.idx]
        if self.ctx.weather.is_rainy:
            text += (" Rain is likely; outdoor stops have an indoor backup." if any(is_outdoor(c) for c in stops)
                     else " Rain is likely, so every stop is indoors.")
        diets = [DIET_LABEL.get(c.value, c.value) for c in self.constraints if c.kind == "diet"]
        if diets and any(is_food_stop(c) for c in stops):
            text += f" Every food stop has confirmed {_join(diets)} options."
        if any(c.kind == "access" for c in self.constraints):
            text += " Sources report accessible entrances; internal facilities and transfer routes need verification."
        if self.crowd_avoid:
            text += " Known busy times were avoided (a heuristic, not live data)."
        if self.min_seated < self.pacing.min_seated_stops and node.seated < self.pacing.min_seated_stops:
            text += " Nothing seated is outdoors, so take rests whenever you like."
        if self._used(node) < 0.5:
            text += " It leaves plenty of free time, since this pace keeps to a few unhurried stops."
        return text[:400]

    def _omissions(self, used: set[str], stops: Sequence[Candidate]) -> list[Omission]:
        out: list[Omission] = []
        no_food = self._no_food_note(stops)
        if no_food:
            out.append(Omission(ref="food stop", reason=no_food))
        scored = []
        for cid, reasons in self.excluded.items():
            c = self.ledger[cid]
            cov = len(covered_interests(c, self.inputs.interests))
            if cov == 0 or cid in used:
                continue
            scored.append((-cov, -(c.rating or 0.0), cid, reasons[0]))
        seen_reasons: set[str] = set()
        for _cov, _rating, cid, reason in sorted(scored):
            text = OMIT_REASONS.get(reason, "Left out to keep the plan valid.")
            if text in seen_reasons:
                continue  # one line per reason, not three identical ones
            seen_reasons.add(text)
            out.append(Omission(ref=cid[:120], reason=text, name=self.ledger[cid].name[:120]))
            if len(out) >= 4:
                break
        return out

    def _no_food_note(self, stops: Sequence[Candidate]) -> str | None:
        """Why a plan under a diet constraint has no food stop, when food was wanted (interest or meal time)."""
        diets = [DIET_LABEL.get(c.value, c.value) for c in self.constraints if c.kind == "diet"]
        if not diets or any(is_food_stop(c) for c in stops):
            return None
        wants_food = any(covered_interests(c, self.inputs.interests) & {"food", "coffee", "dessert"}
                         for c in self.ledger.values() if is_food_stop(c)) or bool(self.main_meal_weight)
        if not wants_food:
            return None
        food = [c for c in self.ledger.values() if is_food_stop(c)]
        diet_blocked = all("diet" in self.excluded.get(c.id, []) for c in food)
        label = _join(diets)
        if diet_blocked:
            return f"No food stop: none of the food places found has confirmed {label} options."
        return f"No food stop: the places with confirmed {label} options don't fit this time window."

    def to_draft(self, node: _Node) -> PlanDraft:
        stops_c = [self.pool[j] for j in node.idx]
        stop_ids = {c.id for c in stops_c}
        legs = [self._leg(node.idx[k - 1], node.idx[k]) for k in range(1, len(node.idx))]
        legs_cost_max = sum(leg.fare.max for leg in legs if leg is not None)
        stop_costs = [c.cost.max for c in stops_c]
        plan_b_costs: list[int | None] = [None] * len(stops_c)
        taken: set[str] = set(stop_ids)
        priciest = max(range(len(stops_c)), key=lambda k: stop_costs[k])
        tight = self.budget is not None and stop_costs[priciest] * 10 >= self.budget * 4 and len(stops_c) > 1
        used_leads: set[str] = set()
        stops: list[DraftStop] = []
        order_pos = 0
        order_broken = False
        for k, (c, dur, start) in enumerate(zip(stops_c, node.durs, node.starts, strict=True)):
            end = start + dur
            plan_b = None
            if self.ctx.weather.is_rainy and is_outdoor(c):
                plan_b = self._plan_b(c, start, end, stop_ids, taken, stop_costs, plan_b_costs, k, legs_cost_max)
                if plan_b:
                    taken.add(plan_b)
                    plan_b_costs[k] = self.ledger[plan_b].cost.max
            order_item = None
            reorder_note = None
            levels = [order_match_at(order_match_level(c, item), item, start) for item in self.order]
            if order_pos < len(self.order) and levels[order_pos]:
                if not order_broken:
                    order_item = self.order[order_pos]
                order_pos += 1
            else:
                later = [i for i in range(order_pos + 1, len(self.order)) if levels[i]]
                if later:
                    order_broken = True
                    reorder_note = self._reorder_note(self.order[order_pos], stops_c[k + 1:])
                    order_pos = later[0] + 1
            why = self._why(c, k, start, end, used_leads, order_item)
            tradeoff = self._tradeoff(c, legs[k - 1] if k > 0 else None, plan_b is not None, tight and k == priciest)
            if reorder_note:
                tradeoff = (reorder_note + (" " + tradeoff if tradeoff else ""))[:240]
            if numeric_claims(why):
                why = "A good fit for the pace and the budget."
            if tradeoff and numeric_claims(tradeoff):
                tradeoff = None
            stops.append(DraftStop(candidate_id=c.id, duration_min=dur, why=why, tradeoff=tradeoff,
                                   plan_b_candidate_id=plan_b))
        return PlanDraft(stops=stops, omitted=self._omissions(stop_ids, stops_c), summary_note=self._summary(node))

    @staticmethod
    def _reorder_note(item: str, rest: Sequence[Candidate]) -> str:
        """Trade-off when the stated order couldn't be kept (e.g. dinner can't come first at four)."""
        label = safe_interest_label(item) or "first choice"
        if any(order_match_level(c, item) for c in rest):
            if item in MEAL_ITEMS:
                return f"The {label} comes later than you asked so it lands at a proper {label} time."
            return f"The {label} comes later than you asked so it fits the opening hours and travel."
        return f"No {label} fits this plan, so it moves straight on to this instead."


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def search_plans(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                 constraints: list[Constraint], travel: TravelEstimator, *, objective: Objective = "best",
                 ignore_budget: bool = False, pacing: PacingProfile | None = None,
                 meal_timing: bool = True) -> SearchResult | None:
    """Best (or cheapest/shortest) valid plan with its validation report, or None. ``pacing`` overrides the
    context's pacing profile (used by infeasible to test whether pacing rules are what binds)."""
    planner = Planner(candidates, ctx, prefs, constraints, travel, ignore_budget=ignore_budget, pacing=pacing,
                      meal_timing=meal_timing)
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

