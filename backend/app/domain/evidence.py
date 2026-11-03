"""Constraint evidence, opening hours and crowd heuristics for a candidate at a scheduled time.

OWNER: domain agent (Phase 1).

Conventions for ``ConstraintCheck``:

* ``satisfied=False`` with a ``code``: the constraint is violated or unverified (validator error).
* ``satisfied=True`` with a ``code``: advisory only (validator warning), e.g. ``ALCOHOL_SERVED`` for a
  restaurant that also serves alcohol under ``alcohol:avoid``.
* ``satisfied=True`` with ``code=None``: satisfied with positive evidence, or not applicable to this stop.

``must_include`` is plan-level: ``check_constraint`` reports it as not applicable per stop; use
``satisfies_include`` and the validator's plan-level check instead.
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

from app.domain.categories import CategoryInfo, load_categories
from app.domain.pacing import MEAL_WINDOWS
from app.domain.schemas import Candidate, Constraint, Evidence, MealWindow, WorldContext
from app.domain.timeutil import overlaps

DAY_START_MIN = 10 * 60  # "most of the day" for the crowd prefilter is judged over at least 10:00-22:00
DAY_END_MIN = 22 * 60
BUSY_MOST_OF_DAY = 0.5

DIET_LABELS = {"vegetarian": "vegetarian", "vegan": "vegan", "jain": "Jain"}


class ConstraintCheck(NamedTuple):
    satisfied: bool  # True only with positive evidence (Evidence.CONFIRMED) or when not applicable
    evidence: Evidence
    code: str | None  # violation code when not satisfied, e.g. "DIET_UNVERIFIED"
    message: str


# ---------------------------------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------------------------------


def category_info(c: Candidate) -> CategoryInfo | None:
    """The candidate's category, or None for keys missing from categories.yaml."""
    return load_categories().get(c.category)


# Event tags or name words that make an event a food stop (FoodEvent, brunches, tastings, food markets...).
FOOD_EVENT_TAGS = frozenset({
    "food", "dining", "dinner", "lunch", "brunch", "breakfast", "tasting", "street food", "dessert", "coffee",
    "food festival", "supper",
})
_FOOD_EVENT_NAME = re.compile(
    r"\b(food|foodie|brunch|breakfast|lunch|dinner|supper|tasting|feast|thali|buffet|grill|bbq|barbecue|kebab|"
    r"biryani|dosa|street food|food market|chef|cook[- ]?off|supper club|pop[- ]?up kitchen)\b",
    re.IGNORECASE,
)


# Same values as app.providers.events_web.APPROX_LOCATION_TAG / APPROX_ADDRESS_PREFIX (kept here so the domain
# never imports providers; a unit test asserts they match).
APPROX_LOCATION_TAG = "approx_location"
APPROX_ADDRESS_PREFIX = "Location approximate - check the venue"


def has_approx_location(c: Candidate) -> bool:
    """The provider had no real coordinates (e.g. an event placed at the city centre)."""
    return APPROX_LOCATION_TAG in {t.lower() for t in c.tags} or (c.address or "").startswith(APPROX_ADDRESS_PREFIX)


def is_food_event(c: Candidate) -> bool:
    """An event that serves food: tagged food (e.g. schema.org FoodEvent) or named after food."""
    if c.kind != "event":
        return False
    if {t.lower() for t in c.tags} & FOOD_EVENT_TAGS:
        return True
    return bool(_FOOD_EVENT_NAME.search(c.name))


def is_food_stop(c: Candidate) -> bool:
    """Diet constraints apply here. Food events count, with the event's own (usually unknown) diet evidence."""
    if is_food_event(c):
        return True
    info = category_info(c)
    if info is None:
        return False
    return info.group == "food" or info.meal or c.category == "brewery"


def is_meal_stop(c: Candidate) -> bool:
    info = category_info(c)
    return bool(info and info.meal)


def is_full_meal(c: Candidate) -> bool:
    """A sit-down meal: a meal category where you sit (restaurant, brewery) and this place has seating.
    Street food and standing-only restaurants are not."""
    info = category_info(c)
    return bool(info and info.meal and info.seated and c.seated)


def is_snack_stop(c: Candidate) -> bool:
    """Food that suits the afternoon snack window: cafes, desserts, street food (not a full meal)."""
    return is_food_stop(c) and not is_full_meal(c)


def main_meal_window(start_min: int, end_min: int) -> MealWindow | None:
    """The breakfast/lunch/dinner window a visit belongs to: it must start inside the window and spend at least
    half of the visit inside it. None when the visit isn't aligned with a main meal."""
    duration = max(1, end_min - start_min)
    for w in MEAL_WINDOWS:
        if w.name == "snack":
            continue
        if w.start_min <= start_min < w.end_min:
            inside = min(end_min, w.end_min) - start_min
            if inside * 2 >= duration:
                return w
    return None


# Venues that are only worth visiting when something is on: unknown hours can't be planned as a stand-alone stop.
PROGRAMME_CATEGORIES = frozenset({"live_music_venue", "theatre", "cinema"})

MEAL_EARLY_MIN = 30  # a sit-down meal may start this much before its window opens (an early lunch/dinner)


def meal_slot(start_min: int, end_min: int, early_min: int = MEAL_EARLY_MIN) -> tuple[MealWindow, int] | None:
    """(main meal window, minutes before it opens) for a sit-down meal at [start, end): it may start up to
    ``early_min`` before breakfast/lunch/dinner opens and must spend at least half of the visit inside the
    window. None when the meal isn't reasonably timed. ``main_meal_window`` is the strict form (no early start)."""
    duration = max(1, end_min - start_min)
    for w in MEAL_WINDOWS:
        if w.name == "snack":
            continue
        if w.start_min - early_min <= start_min < w.end_min:
            inside = min(end_min, w.end_min) - max(start_min, w.start_min)
            if inside * 2 >= duration:
                return w, max(0, w.start_min - start_min)
    return None


def calm_at(c: Candidate, start_min: int, end_min: int) -> bool:
    """True only when a calm claim is backed: low popularity and no overlap with the stop's busy windows."""
    return c.popularity == "low" and not any(overlaps(start_min, end_min, ws, we) for ws, we in c.busy_windows)


def is_outdoor(c: Candidate) -> bool:
    """True when the candidate is outdoors (falls back to the category when the candidate is silent)."""
    if c.indoor is not None:
        return not c.indoor
    info = category_info(c)
    return bool(info and not info.indoor)


def is_walking_stop(c: Candidate) -> bool:
    info = category_info(c)
    return bool(info and info.walking)


def all_tags(c: Candidate) -> set[str]:
    info = category_info(c)
    tags = {t.lower() for t in c.tags}
    if info:
        tags |= {t.lower() for t in info.tags}
    return tags


def event_window(c: Candidate) -> tuple[int, int] | None:
    """[start, end] of an event; the end defaults to start + the candidate's max duration."""
    if c.event_start_min is None:
        return None
    end = c.event_end_min if c.event_end_min is not None else c.event_start_min + c.duration_range[2]
    return c.event_start_min, end


# ---------------------------------------------------------------------------------------------------
# Hours and crowds
# ---------------------------------------------------------------------------------------------------


def _normalized_periods(c: Candidate) -> list[tuple[int, int]]:
    out = []
    for open_min, close_min in c.hours.periods:
        if close_min <= open_min:  # closes after midnight, e.g. (1080, 120)
            close_min += 24 * 60
        out.append((open_min, close_min))
    return out


def opening_status(c: Candidate, start_min: int, end_min: int) -> Literal["open", "closed", "unknown"]:
    """"open" if [start, end) fits inside one known period; "closed" if hours are known and it doesn't;
    "unknown" if hours are unknown. Events are "open" exactly within [event_start_min, event_end_min]."""
    if c.kind == "event":
        window = event_window(c)
        if window is None:
            return "unknown"
        return "open" if window[0] <= start_min and end_min <= window[1] else "closed"
    if c.hours.status != "known":
        return "unknown"
    for open_min, close_min in _normalized_periods(c):
        if open_min <= start_min and end_min <= close_min:
            return "open"
    return "closed"


def closes_at(c: Candidate, start_min: int, end_min: int) -> int | None:
    """When the visit [start, end) has to end at the latest: the close of the known period containing it, or the
    event's end. None when hours are unknown (no limit known) or the visit isn't inside a period."""
    if c.kind == "event":
        window = event_window(c)
        return window[1] if window is not None else None
    if c.hours.status != "known":
        return None
    for open_min, close_min in _normalized_periods(c):
        if open_min <= start_min and end_min <= close_min:
            return close_min
    return None


def open_overlap_minutes(c: Candidate, start_min: int, end_min: int) -> int | None:
    """Longest stretch of known opening inside [start, end); None when hours are unknown."""
    if c.kind == "event":
        window = event_window(c)
        if window is None:
            return None
        return max(0, min(end_min, window[1]) - max(start_min, window[0]))
    if c.hours.status != "known":
        return None
    best = 0
    for open_min, close_min in _normalized_periods(c):
        best = max(best, min(end_min, close_min) - max(start_min, open_min))
    return best


def likely_busy(c: Candidate, start_min: int, end_min: int) -> bool:
    """Heuristic: popularity == "high" and the visit overlaps any busy window."""
    if c.popularity != "high":
        return False
    return any(overlaps(start_min, end_min, ws, we) for ws, we in c.busy_windows)


def busy_most_of_day(c: Candidate, ctx: WorldContext) -> bool:
    """High-popularity candidate whose busy windows cover more than half of the day (crowd prefilter)."""
    if c.popularity != "high" or not c.busy_windows:
        return False
    day_start = min(ctx.start_min, DAY_START_MIN)
    day_end = max(ctx.end_min, DAY_END_MIN)
    clipped = sorted((max(day_start, s), min(day_end, e)) for s, e in c.busy_windows if e > day_start and s < day_end)
    covered, cur_s, cur_e = 0, None, None
    for s, e in clipped:
        if cur_e is None or s > cur_e:
            if cur_e is not None and cur_s is not None:
                covered += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None and cur_s is not None:
        covered += cur_e - cur_s
    return covered > BUSY_MOST_OF_DAY * (day_end - day_start)


# ---------------------------------------------------------------------------------------------------
# Hard constraints
# ---------------------------------------------------------------------------------------------------


def _ok(message: str = "", evidence: Evidence = Evidence.CONFIRMED) -> ConstraintCheck:
    return ConstraintCheck(True, evidence, None, message)


def _na() -> ConstraintCheck:
    return ConstraintCheck(True, Evidence.CONFIRMED, None, "not applicable")


def _diet_check(c: Candidate, diet: str) -> ConstraintCheck:
    if not is_food_stop(c):
        return _na()
    d = c.diet
    veg = d.get("vegetarian", Evidence.UNKNOWN)
    vegan = d.get("vegan", Evidence.UNKNOWN)
    jain = d.get("jain", Evidence.UNKNOWN)
    label = DIET_LABELS.get(diet, diet)
    if diet == "vegetarian":
        if Evidence.CONFIRMED in (veg, vegan, jain):
            return _ok("vegetarian confirmed")
        own = veg
        conflict = veg == Evidence.NO
    elif diet == "vegan":
        own = vegan
        if vegan == Evidence.CONFIRMED:
            return _ok("vegan confirmed")
        conflict = Evidence.NO in (vegan, veg)
    elif diet == "jain":
        own = jain
        if jain == Evidence.CONFIRMED:
            return _ok("Jain confirmed")
        conflict = Evidence.NO in (jain, veg)
    else:
        return ConstraintCheck(False, Evidence.UNKNOWN, "DIET_UNVERIFIED", f"Unknown diet requirement '{diet}'")
    if conflict:
        return ConstraintCheck(False, Evidence.NO, "DIET_CONFLICT", f"{c.name} is known not to suit a {label} diet")
    return ConstraintCheck(
        False, own if own != Evidence.NO else Evidence.UNKNOWN, "DIET_UNVERIFIED",
        f"{label.capitalize()} options at {c.name} are not confirmed",
    )


def satisfies_include(c: Candidate, value: str) -> bool:
    """True if this candidate alone meets ``must_include:<value>``."""
    info = category_info(c)
    tags = all_tags(c)
    cat = c.category
    if value == "sit_down_meal":
        return is_full_meal(c)
    if value == "outdoor_walk":
        return bool(info and info.walking) and c.indoor is False
    if value == "live_music":
        return bool(c.live_music) or cat == "live_music_venue" or (c.kind == "event" and "music" in tags)
    if value == "coffee":
        return cat == "cafe"
    if value == "museum":
        return cat == "museum"
    if value == "art":
        return cat in {"art_gallery", "museum"} or "art" in tags
    if value == "books":
        return cat == "bookstore"
    if value == "park":
        return cat in {"park", "garden", "lake"}
    if value == "event":
        return c.kind == "event"
    if value == "shopping":
        return cat in {"mall", "market", "bookstore"}
    if value == "dessert":
        return cat == "dessert"
    return False


def matches_avoid(c: Candidate, value: str) -> bool:
    """True if this candidate falls under ``must_avoid:<value>`` (category key, group, or "alcohol")."""
    info = category_info(c)
    if value == "alcohol":
        return bool(info and info.alcohol) or c.serves_alcohol is True
    if c.category == value:
        return True
    return bool(info and info.group == value)


def check_constraint(c: Candidate, constraint: Constraint, start_min: int, end_min: int,
                     ctx: WorldContext) -> ConstraintCheck:
    """Evaluate one hard constraint for a stop scheduled at [start, end). Unknown never satisfies."""
    kind, value = constraint.kind, constraint.value
    if kind == "diet":
        return _diet_check(c, value)
    if kind == "access":
        if value != "wheelchair":
            return ConstraintCheck(False, Evidence.UNKNOWN, "ACCESSIBILITY_UNVERIFIED", f"Unknown access need '{value}'")
        if c.wheelchair == Evidence.CONFIRMED:
            return _ok("wheelchair access confirmed")
        if c.wheelchair == Evidence.NO:
            return ConstraintCheck(False, Evidence.NO, "ACCESSIBILITY_UNVERIFIED",
                                   f"{c.name} is reported as not wheelchair accessible")
        return ConstraintCheck(False, c.wheelchair, "ACCESSIBILITY_UNVERIFIED",
                               f"Wheelchair access at {c.name} is not confirmed")
    if kind == "alcohol":
        info = category_info(c)
        if info and info.alcohol:
            return ConstraintCheck(False, Evidence.NO, "ALCOHOL_CONFLICT", f"{c.name} is an alcohol-focused venue")
        if c.serves_alcohol is True:
            return ConstraintCheck(True, Evidence.LIKELY, "ALCOHOL_SERVED",
                                   f"{c.name} serves alcohol, though that is not its focus")
        return _ok("not an alcohol venue")
    if kind == "setting":
        if value == "indoors_only":
            if c.indoor is True:
                return _ok("indoors")
            if c.indoor is False:
                return ConstraintCheck(False, Evidence.NO, "SETTING_CONFLICT", f"{c.name} is outdoors")
            return ConstraintCheck(False, Evidence.UNKNOWN, "SETTING_CONFLICT",
                                   f"Not confirmed that {c.name} is indoors")
        if value == "outdoors_only":
            if c.indoor is False:
                return _ok("outdoors")
            if c.indoor is True:
                return ConstraintCheck(False, Evidence.NO, "SETTING_CONFLICT", f"{c.name} is indoors")
            return ConstraintCheck(False, Evidence.UNKNOWN, "SETTING_CONFLICT",
                                   f"Not confirmed that {c.name} is outdoors")
        return ConstraintCheck(False, Evidence.UNKNOWN, "SETTING_CONFLICT", f"Unknown setting '{value}'")
    if kind == "crowd":
        if likely_busy(c, start_min, end_min):
            return ConstraintCheck(False, Evidence.LIKELY, "CROWD_LIKELY",
                                   f"{c.name} is likely busy at that time (heuristic)")
        return _ok("not likely busy at that time (heuristic)", Evidence.LIKELY)
    if kind == "must_avoid":
        if matches_avoid(c, value):
            return ConstraintCheck(False, Evidence.NO, "MUST_AVOID_VIOLATED",
                                   f"{c.name} is a '{value.replace('_', ' ')}' stop you asked to avoid")
        return _ok()
    if kind == "must_include":
        return _na()  # plan-level; see satisfies_include
    return _na()


_TIME_INDEPENDENT_KINDS = {"diet", "access", "alcohol", "setting", "must_avoid"}


def candidate_allowed(c: Candidate, constraints: list[Constraint], ctx: WorldContext) -> tuple[bool, list[str]]:
    """Time-independent prefilter: (allowed, reasons). Excludes known-violating candidates (e.g. non-veg
    food for diet:vegetarian, alcohol venues for alcohol:avoid, outdoor for indoors_only).

    Because unknown evidence never satisfies a hard constraint, candidates whose evidence is unknown for a
    time-independent constraint that applies to them are excluded too (they could never pass validation).
    With ``crowd:avoid``, high-popularity candidates that are busy most of the day are excluded. Reasons are
    short plain sentences without numbers. Advisory checks (e.g. a restaurant that serves alcohol) don't
    exclude.
    """
    reasons: list[str] = []
    for con in constraints:
        if con.kind in _TIME_INDEPENDENT_KINDS:
            chk = check_constraint(c, con, ctx.start_min, ctx.end_min, ctx)
            if not chk.satisfied:
                reasons.append(chk.message)
        elif con.kind == "crowd" and con.value == "avoid" and busy_most_of_day(c, ctx):
            reasons.append(f"{c.name} is likely busy for most of the day (heuristic)")
    return (not reasons, reasons)


def failing_constraints(c: Candidate, constraints: list[Constraint], ctx: WorldContext) -> list[Constraint]:
    """The constraints for which ``candidate_allowed`` would exclude ``c`` (used to explain infeasibility)."""
    out: list[Constraint] = []
    for con in constraints:
        ok, _ = candidate_allowed(c, [con], ctx)
        if not ok:
            out.append(con)
    return out
