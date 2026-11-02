"""Candidate scoring for the deterministic planner. OWNER: domain agent (Phase 1).

Scores are unitless and only used for ranking. Components: interest-tag overlap (with a small synonym map),
energy fit (seated / low effort for low energy), rating, crowd penalty under ``crowd:avoid``, indoor bias when
rain is likely, meal-window fit for meal stops, distance from the neighbourhood (or the candidates' centroid),
a mild cost penalty relative to the budget, a clear penalty for unknown opening hours, mood words (romantic,
family, artsy, quiet...) and a preference for events with real coordinates.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.evidence import all_tags, has_approx_location, is_meal_stop, is_outdoor
from app.domain.schemas import Candidate, Constraint, Energy, MealWindow, Preferences, WorldContext
from app.domain.travel import coords_valid, haversine_km

# interest word -> candidate tags that satisfy it
INTEREST_SYNONYMS: dict[str, frozenset[str]] = {
    "walks": frozenset({"walks", "nature", "outdoors"}),
    "walk": frozenset({"walks", "nature", "outdoors"}),
    "walking": frozenset({"walks", "nature", "outdoors"}),
    "nature": frozenset({"nature", "walks", "outdoors", "flowers"}),
    "outdoors": frozenset({"outdoors", "walks", "nature"}),
    "parks": frozenset({"walks", "nature", "outdoors"}),
    "music": frozenset({"music", "live music"}),
    "live music": frozenset({"live music", "music"}),
    "gigs": frozenset({"live music", "music"}),
    "food": frozenset({"food", "coffee", "dessert", "street food"}),
    "eating": frozenset({"food", "coffee", "dessert", "street food"}),
    "street food": frozenset({"street food"}),
    "coffee": frozenset({"coffee"}),
    "cafes": frozenset({"coffee"}),
    "cafe": frozenset({"coffee"}),
    "dessert": frozenset({"dessert"}),
    "desserts": frozenset({"dessert"}),
    "books": frozenset({"books"}),
    "bookshops": frozenset({"books"}),
    "bookstores": frozenset({"books"}),
    "reading": frozenset({"books"}),
    "art": frozenset({"art"}),
    "arts": frozenset({"art"}),
    "artsy": frozenset({"art"}),
    "museums": frozenset({"museums", "history"}),
    "museum": frozenset({"museums", "history"}),
    "history": frozenset({"history", "museums"}),
    "culture": frozenset({"culture", "art", "history"}),
    "theatre": frozenset({"theatre"}),
    "theater": frozenset({"theatre"}),
    "movies": frozenset({"movies"}),
    "films": frozenset({"movies"}),
    "shopping": frozenset({"shopping"}),
    "nightlife": frozenset({"nightlife"}),
    "drinks": frozenset({"drinks"}),
    "relax": frozenset({"relax", "wellness"}),
    "relaxing": frozenset({"relax", "wellness"}),
    "wellness": frozenset({"wellness", "relax"}),
    "games": frozenset({"games"}),
    "gaming": frozenset({"games"}),
    "views": frozenset({"views"}),
    "events": frozenset({"events"}),
}

_SAFE_INTEREST = re.compile(r"^[a-z][a-z \-']{0,28}[a-z]$")


def interest_tags(interest: str) -> frozenset[str]:
    key = interest.strip().lower()
    if key in INTEREST_SYNONYMS:
        return INTEREST_SYNONYMS[key]
    if key.endswith("s") and key[:-1] in INTEREST_SYNONYMS:
        return INTEREST_SYNONYMS[key[:-1]]
    if key + "s" in INTEREST_SYNONYMS:
        return INTEREST_SYNONYMS[key + "s"]
    variants = {key}
    if key.endswith("s"):
        variants.add(key[:-1])
    else:
        variants.add(key + "s")
    return frozenset(variants)


def safe_interest_label(interest: str) -> str | None:
    """A lowercase interest safe to echo in template prose (letters only), or None."""
    label = interest.strip().lower()
    return label if _SAFE_INTEREST.match(label) else None


def candidate_tags(c: Candidate) -> set[str]:
    tags = all_tags(c)
    if c.live_music:
        tags |= {"music", "live music"}
    if c.kind == "event":
        tags.add("events")
    return tags


def covered_interests(c: Candidate, interests: Sequence[str]) -> set[str]:
    """Which of the user's interests (original strings) this candidate covers."""
    tags = candidate_tags(c)
    return {i for i in interests if interest_tags(i) & tags}


# mood words -> candidate tags that suit the mood (candidate tags come from providers and categories.yaml)
MOOD_TAGS: list[tuple[re.Pattern[str], frozenset[str]]] = [
    (re.compile(r"romantic|date night|\bdate\b|partner|anniversary|couple|girlfriend|boyfriend|wife|husband"),
     frozenset({"romantic", "views", "live music", "music", "dessert", "fine dining"})),
    (re.compile(r"famil|kids?\b|children|child"), frozenset({"family", "kids", "nature", "parks", "museums"})),
    (re.compile(r"arty|artsy|creative|inspired"), frozenset({"art", "culture"})),
    (re.compile(r"adventur|active|outdoorsy"), frozenset({"outdoors", "games", "walks"})),
    (re.compile(r"chill|relax|lazy|cosy|cozy|unwind"), frozenset({"relax", "coffee", "books"})),
]
_QUIET = re.compile(r"quiet|calm|peaceful|tranquil|serene|low[- ]key|not too (busy|crowded)|away from (the )?crowd")


def mood_profile(mood: str) -> tuple[frozenset[str], bool]:
    """(tags the mood favours, wants quiet places) from free-text mood words."""
    text = (mood or "").lower()
    tags: set[str] = set()
    for pattern, favoured in MOOD_TAGS:
        if pattern.search(text):
            tags |= favoured
    return frozenset(tags), bool(_QUIET.search(text))


@dataclass(frozen=True)
class ScoreInputs:
    interests: tuple[str, ...]
    energy: Energy
    crowd_avoid: bool
    rainy: bool
    anchor: tuple[float, float] | None
    meal_windows: tuple[MealWindow, ...]
    budget: int | None
    mood_tags: frozenset[str] = frozenset()
    quiet: bool = False


def anchor_point(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences) -> tuple[float, float] | None:
    """Neighbourhood centroid when candidates name it, else the candidates' median point, else the city."""
    pts = [c for c in candidates if coords_valid(c.lat, c.lng)]
    if prefs.neighbourhood:
        hood = prefs.neighbourhood.strip().lower()
        near = [c for c in pts if (c.neighbourhood and hood in c.neighbourhood.lower())
                or (c.address and hood in c.address.lower())]
        if near:
            return statistics.median(c.lat for c in near), statistics.median(c.lng for c in near)
    if pts:
        return statistics.median(c.lat for c in pts), statistics.median(c.lng for c in pts)
    if coords_valid(ctx.location.lat, ctx.location.lng):
        return ctx.location.lat, ctx.location.lng
    return None


def build_inputs(candidates: Sequence[Candidate], ctx: WorldContext, prefs: Preferences,
                 constraints: Sequence[Constraint]) -> ScoreInputs:
    keys = {c.key for c in constraints}
    mood_tags, quiet = mood_profile(prefs.mood)
    return ScoreInputs(
        interests=tuple(i for i in prefs.interests if i.strip()),
        energy=prefs.energy,
        crowd_avoid="crowd:avoid" in keys,
        rainy=ctx.weather.is_rainy,
        anchor=anchor_point(candidates, ctx, prefs),
        meal_windows=tuple(w for w in ctx.meal_windows if w.name in {"breakfast", "lunch", "dinner"}),
        budget=prefs.budget,
        mood_tags=mood_tags,
        quiet=quiet,
    )


_EFFORT = {
    Energy.LOW: {"low": 1.0, "medium": -0.4, "high": -2.0},
    Energy.MEDIUM: {"low": 0.3, "medium": 0.2, "high": -0.4},
    Energy.HIGH: {"low": 0.0, "medium": 0.4, "high": 0.6},
}
_CROWD = {"high": -2.5, "medium": -0.3, "low": 0.4}


def score_candidate(c: Candidate, inputs: ScoreInputs) -> float:
    s = 3.0 * len(covered_interests(c, inputs.interests))
    s += _EFFORT[inputs.energy][c.effort]
    if inputs.energy == Energy.LOW and c.seated:
        s += 1.5
    if c.rating is not None:
        s += max(-2.0, min(1.5, (c.rating - 4.0) * 1.5))
        if c.rating_count is not None and c.rating_count < 20:
            s -= 0.3
    if inputs.crowd_avoid:
        s += _CROWD[c.popularity]
    if inputs.rainy:
        if c.indoor is True:
            s += 1.5
        elif is_outdoor(c):
            s -= 2.5
    if is_meal_stop(c) and inputs.meal_windows:
        s += 1.0
    if inputs.anchor is not None and coords_valid(c.lat, c.lng):
        km = haversine_km(c.lat, c.lng, *inputs.anchor)
        s -= min(3.0, 0.35 * max(0.0, km - 1.5))
    if inputs.budget:
        s -= 1.5 * min(1.0, c.cost.max / inputs.budget)
    if c.kind == "place" and c.hours.status != "known":
        s -= 2.0  # verified hours should win; unknown hours are only a fallback
    if c.kind == "event":
        s += 0.8
    if has_approx_location(c):
        s -= 2.5  # prefer events with real coordinates (legs to this one are guesses)
    tags = candidate_tags(c)
    if inputs.mood_tags:
        s += min(3.0, 1.5 * len(inputs.mood_tags & tags))
    if tags & {"kids", "children"} and not inputs.mood_tags & {"kids", "family"}:
        s -= 2.0  # a kids' park is not an adult's first pick
    if inputs.quiet:
        s += {"low": 1.0, "medium": 0.0, "high": -1.5}[c.popularity]
        if "nightlife" in candidate_tags(c):
            s -= 1.0
    return s
