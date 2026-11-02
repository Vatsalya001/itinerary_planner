"""Shared normalization helpers for every places/events/weather provider.

Pure functions only (no I/O beyond reading ``data/price_bands.yaml`` once). Units follow the schemas:
local minutes since midnight on the target date (may exceed 1440), whole currency units per person.

Also hosts the keyword search used by the fixture and snapshot providers (``search_local``), so both
offline providers have identical, deterministic search semantics.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import yaml

from app.domain.categories import DATA_DIR, category, currency_for_country, is_category, load_categories
from app.domain.parser_rules import lookup_place
from app.domain.schemas import Candidate, CostRange, Evidence, Hours, Location, PlaceQuery, WorldContext

Popularity = Literal["low", "medium", "high"]

CITY_RADIUS_KM = 40.0  # candidates further than this from the resolved city centre are out of city
DAY = 1440
ALWAYS_OPEN: tuple[int, int] = (0, 2 * DAY)  # "24/7": open all of the target day and the night after

PRICE_LEVELS: tuple[str, ...] = (
    "PRICE_LEVEL_FREE",
    "PRICE_LEVEL_INEXPENSIVE",
    "PRICE_LEVEL_MODERATE",
    "PRICE_LEVEL_EXPENSIVE",
    "PRICE_LEVEL_VERY_EXPENSIVE",
)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def within_city(lat: float, lng: float, location: Location, radius_km: float = CITY_RADIUS_KM) -> bool:
    return haversine_m(lat, lng, location.lat, location.lng) <= radius_km * 1000


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

# Google Places API (New) place types (Table A) -> canonical category. Order inside the dict is irrelevant;
# the caller's type order (primaryType first) decides precedence.
_GOOGLE_TYPE_MAP: dict[str, str] = {
    # cafes
    "cafe": "cafe", "coffee_shop": "cafe", "tea_house": "cafe", "bakery": "cafe", "cafeteria": "cafe",
    "juice_shop": "cafe", "cat_cafe": "cafe", "dog_cafe": "cafe", "internet_cafe": "cafe",
    # dessert
    "ice_cream_shop": "dessert", "dessert_shop": "dessert", "dessert_restaurant": "dessert",
    "confectionery": "dessert", "chocolate_shop": "dessert", "candy_store": "dessert", "donut_shop": "dessert",
    "chocolate_factory": "dessert",
    # street food / quick food
    "fast_food_restaurant": "street_food", "food_court": "street_food", "sandwich_shop": "street_food",
    "snack_bar": "street_food", "street_food": "street_food",
    # drinks
    "bar": "bar", "pub": "bar", "wine_bar": "bar", "cocktail_bar": "bar", "lounge_bar": "bar",
    "sports_bar": "bar", "night_club": "bar", "hookah_bar": "bar", "irish_pub": "bar", "bar_and_grill": "bar",
    "beer_garden": "brewery", "brewery": "brewery", "brewpub": "brewery",
    # entertainment
    "live_music_venue": "live_music_venue", "concert_hall": "live_music_venue", "opera_house": "live_music_venue",
    "philharmonic_hall": "live_music_venue", "amphitheatre": "live_music_venue", "karaoke": "live_music_venue",
    "performing_arts_theater": "theatre", "auditorium": "theatre", "comedy_club": "theatre",
    "cultural_center": "theatre", "dance_hall": "theatre",
    "movie_theater": "cinema", "drive_in": "cinema",
    "amusement_center": "game_arcade", "video_arcade": "game_arcade", "bowling_alley": "game_arcade",
    "amusement_park": "game_arcade", "escape_room": "game_arcade",
    # culture
    "museum": "museum", "history_museum": "museum", "planetarium": "museum", "aquarium": "museum",
    "science_museum": "museum", "art_museum": "museum", "children_museum": "museum",
    "art_gallery": "art_gallery", "art_studio": "art_gallery", "sculpture": "art_gallery",
    "book_store": "bookstore", "library": "bookstore",
    "church": "place_of_worship", "hindu_temple": "place_of_worship", "mosque": "place_of_worship",
    "synagogue": "place_of_worship", "place_of_worship": "place_of_worship", "buddhist_temple": "place_of_worship",
    "gurudwara": "place_of_worship",
    # outdoors
    "park": "park", "city_park": "park", "national_park": "park", "state_park": "park", "dog_park": "park",
    "playground": "park", "picnic_ground": "park", "plaza": "park",
    "garden": "garden", "botanical_garden": "garden",
    "lake": "lake",
    "historical_landmark": "heritage_walk", "historical_place": "heritage_walk", "monument": "heritage_walk",
    "cultural_landmark": "heritage_walk",
    "observation_deck": "viewpoint", "scenic_spot": "viewpoint", "vista_point": "viewpoint",
    # shopping
    "market": "market", "farmers_market": "market", "flea_market": "market", "food_market": "market",
    "shopping_mall": "mall", "department_store": "mall",
    # wellness
    "spa": "spa", "massage": "spa", "wellness_center": "spa", "sauna": "spa", "massage_spa": "spa",
}


def google_types_to_categories(primary_type: str | None, types: Sequence[str] | None) -> list[str]:
    """Every canonical category the place's types map to, in precedence order (primary type first).

    ``*_restaurant`` types not listed explicitly map to ``restaurant``; generic types are ignored."""
    ordered: list[str] = []
    for t in [primary_type, *(types or [])]:
        if not t or not isinstance(t, str):
            continue
        key = _GOOGLE_TYPE_MAP.get(t)
        if key is None and (t == "restaurant" or t.endswith("_restaurant") or t in {"diner", "steak_house"}):
            key = "restaurant"
        if key and key not in ordered:
            ordered.append(key)
    return ordered


def google_types_to_category(primary_type: str | None, types: Sequence[str] | None = None) -> str | None:
    """Canonical category for a Places API (New) place, or None when no type maps."""
    found = google_types_to_categories(primary_type, types)
    return found[0] if found else None


def categories_for_filter(value: str | None) -> set[str] | None:
    """A query ``category`` may be a category key or a group name (food, outdoors...). None = no filter."""
    if not value:
        return None
    if is_category(value):
        return {value}
    group = {k for k, info in load_categories().items() if info.group == value}
    return group or None


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_price_bands() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((DATA_DIR / "price_bands.yaml").read_text(encoding="utf-8"))
    return raw


def location_currency(location: Location) -> str:
    """The currency candidates for this location are priced in."""
    return location.currency or currency_for_country(location.country_code) or "USD"


def price_level_index(level: str | int | None) -> int | None:
    """``PRICE_LEVEL_MODERATE`` -> 2; ints 0..4 pass through; anything else -> None."""
    if level is None:
        return None
    if isinstance(level, int):
        return level if 0 <= level < len(PRICE_LEVELS) else None
    try:
        return PRICE_LEVELS.index(level)
    except ValueError:
        return None


def _bands_for(currency: str) -> tuple[str, dict[str, list[int]]]:
    bands: dict[str, dict[str, list[int]]] = load_price_bands()["bands"]
    if currency in bands:
        return currency, bands[currency]
    return "USD", bands["USD"]  # unknown currency: fall back to USD bands, priced in USD


def price_level_band(level: str | int | None, currency: str) -> CostRange | None:
    """Per-person estimate for a Google ``priceLevel`` (basis ``price_level_estimate``); None if unknown.

    Currencies without a band table fall back to USD bands and are priced in USD."""
    idx = price_level_index(level)
    if idx is None:
        return None
    cur, bands = _bands_for(currency)
    lo, hi = bands[PRICE_LEVELS[idx]]
    return CostRange(min=lo, max=hi, currency=cur, basis="price_level_estimate")


def price_level_cap(level: int, currency: str) -> int:
    """Upper per-person bound of a price level (used to apply ``PlaceQuery.max_price_level`` as a cost cap)."""
    band = price_level_band(level, currency)
    assert band is not None
    return band.max


def category_default_cost(category_key: str, currency: str) -> CostRange:
    """Category default per person: ``cost_inr`` for INR, otherwise the band table's category levels.

    Basis is ``category_estimate``; categories whose default is zero (parks, lakes, walks) are ``free``."""
    info = category(category_key)
    if currency == "INR":
        lo, hi = info.cost_inr
        cur = "INR"
    else:
        levels: dict[str, list[str]] = load_price_bands()["category_levels"]
        low_level, high_level = levels.get(category_key, ["PRICE_LEVEL_INEXPENSIVE", "PRICE_LEVEL_MODERATE"])
        cur, bands = _bands_for(currency)
        lo, hi = bands[low_level][0], bands[high_level][1]
    return CostRange(min=lo, max=hi, currency=cur, basis="free" if hi == 0 else "category_estimate")


# ---------------------------------------------------------------------------
# Popularity heuristic
# ---------------------------------------------------------------------------


def popularity_from_rating_count(count: int | None) -> Popularity:
    """Crowd heuristic from review volume: >=5000 high, >=800 medium, else low. Unknown -> medium
    (never "low": an unknown crowd level must not look like a quiet place)."""
    if count is None:
        return "medium"
    if count >= 5000:
        return "high"
    if count >= 800:
        return "medium"
    return "low"


def busy_windows_for(category_key: str, popularity: Popularity) -> list[tuple[int, int]]:
    """Likely-busy Saturday windows (heuristic). Low-popularity places get none; others get the category's
    typical windows. The domain's ``likely_busy`` only flags high-popularity stops inside these windows."""
    if popularity == "low":
        return []
    return [(int(a), int(b)) for a, b in category(category_key).busy]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def maps_search_url(name: str, lat: float, lng: float, place_id: str | None = None) -> str:
    """Google Maps search URL (no key needed). With a place ID the name is the query and the ID pins the
    exact place; without one the coordinates are the query so the pin is exact even for unknown names."""
    params = {"api": "1"}
    if place_id:
        params["query"] = name
        params["query_place_id"] = place_id
    else:
        params["query"] = f"{lat:.6f},{lng:.6f}"
    return "https://www.google.com/maps/search/?" + urlencode(params)


def is_https_url(url: object) -> bool:
    """True only for a plain ``https://host/...`` URL: non-empty host, no userinfo ("user@host" tricks),
    no whitespace or control characters, at most 2048 characters."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url) or "\\" in url:
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or not parts.netloc or "@" in parts.netloc:
        return False
    host = parts.hostname or ""
    return bool(host) and not host.startswith(".") and ".." not in host


def maps_text_search_url(text: str) -> str:
    """Google Maps search URL for free text (used when coordinates are only approximate)."""
    return "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": text})


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------

_OSM_DAYS = ["mo", "tu", "we", "th", "fr", "sa", "su"]
_TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})(\+)?$")


def merge_periods(periods: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(periods):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _parse_days(spec: str) -> set[int] | None:
    days: set[int] = set()
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part == "ph" or part == "sh":
            continue  # public/school holidays: ignored (we plan ordinary Saturdays)
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in _OSM_DAYS or b not in _OSM_DAYS:
                return None
            i, j = _OSM_DAYS.index(a), _OSM_DAYS.index(b)
            k = i
            while True:
                days.add(k)
                if k == j:
                    break
                k = (k + 1) % 7
        elif part in _OSM_DAYS:
            days.add(_OSM_DAYS.index(part))
        else:
            return None
    return days


def _parse_times(spec: str) -> list[tuple[int, int]] | None:
    periods: list[tuple[int, int]] = []
    for part in spec.split(","):
        m = _TIME_RANGE.match(part.strip())
        if not m:
            return None
        h1, m1, h2, m2, plus = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5]
        if h1 > 24 or h2 > 48 or m1 > 59 or m2 > 59:
            return None
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end <= start:
            end += DAY  # overnight, e.g. 18:00-02:00
        if plus:
            end = max(end, start + 60)  # "open end": assume at least an hour, never longer
        periods.append((start, end))
    return periods


def join_next_day(today: list[tuple[int, int]], next_day: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Continue periods that reach midnight into the next day's periods that open at 00:00.

    ``next_day`` periods are relative to the next day's midnight. "Open 00:00-24:00 every day" thus becomes
    one period running past midnight, so a 23:30-00:30 visit is not rejected."""
    early = [end for start, end in next_day if start == 0 and end > 0]
    if not early:
        return today
    reach = DAY + max(early)
    return [(s, max(e, reach)) if e >= DAY else (s, e) for s, e in today]


def parse_osm_opening_hours(text: str | None, weekday: int) -> Hours:
    """Parse common OSM ``opening_hours`` values for one weekday (Python convention, 0 = Monday).

    Supports "24/7", "off"/"closed", "Mo-Su 06:00-18:00", "Mo-Fr 09:00-17:00; Sa 10:00-14:00",
    "Tu-Su 10:00-17:00" (other days closed), bare "10:00-22:00" (every day), several ranges
    ("12:30-16:00, 19:00-23:00"), overnight ranges ("18:00-02:00" -> close > 1440), day lists
    ("Mo,We,Fr") and "<days> off" rules. Later rules override earlier ones for the days they name.
    Anything else (months, sunrise, comments, week numbers...) returns ``Hours(status="unknown")``.
    """
    if not text or not text.strip():
        return Hours(status="unknown")
    raw = text.strip()
    if raw == "24/7":
        return Hours(status="known", periods=[ALWAYS_OPEN])
    if raw.lower() in {"off", "closed"}:
        return Hours(status="known", periods=[])
    if any(ch in raw for ch in '"[]') or re.search(r"[a-z]{3,}", raw.lower().replace("off", "").replace("closed", "")):
        return Hours(status="unknown")  # comments, month names, sunrise/sunset, "week", "easter"...

    per_day: dict[int, list[tuple[int, int]]] = {}
    for rule in raw.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        if re.fullmatch(r"(?i)ph(\s+(off|closed))?|ph\s+.*", rule):
            continue  # holiday-only rule
        m = re.match(r"^((?:[A-Za-z]{2}(?:\s*-\s*[A-Za-z]{2})?\s*,?\s*)+)\s+(.*)$", rule)
        if m and _parse_days(m[1].replace(" ", "")) is not None:
            day_spec, rest = m[1].replace(" ", ""), m[2].strip()
            days = _parse_days(day_spec)
        elif re.fullmatch(r"(?i)[a-z]{2}(\s*-\s*[a-z]{2})?(,[a-z]{2}(\s*-\s*[a-z]{2})?)*", rule):
            return Hours(status="unknown")  # days without times
        else:
            days, rest = set(range(7)), rule
        if days is None:
            return Hours(status="unknown")
        if rest.lower() in {"off", "closed"}:
            periods: list[tuple[int, int]] = []
        elif rest == "00:00-24:00" or rest == "24/7":
            periods = [(0, DAY)]
        else:
            parsed = _parse_times(rest)
            if parsed is None:
                return Hours(status="unknown")
            periods = parsed
        for d in days:
            per_day[d] = list(periods)

    today = list(per_day.get(weekday, []))
    # A previous-day overnight range (e.g. Fr 18:00-02:00) spills into the early hours of this day.
    for _start, end in per_day.get((weekday - 1) % 7, []):
        if end > DAY:
            today.append((0, end - DAY))
    # A period ending at midnight continues into the next day when that day opens at 00:00.
    today = join_next_day(today, per_day.get((weekday + 1) % 7, []))
    return Hours(status="known", periods=merge_periods(today))


def is_open_at(hours: Hours, minute: int) -> bool | None:
    """True/False when hours are known, None when unknown."""
    if hours.status != "known":
        return None
    return any(start <= minute < end for start, end in hours.periods)


def latest_close(hours: Hours) -> int | None:
    if hours.status != "known" or not hours.periods:
        return None
    return max(end for _, end in hours.periods)


# ---------------------------------------------------------------------------
# Weather text
# ---------------------------------------------------------------------------

_WMO: dict[int, str] = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Rain showers", 82: "Violent rain showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}


def wmo_description(code: int | None) -> str:
    if code is None:
        return "Weather"
    return _WMO.get(int(code), "Mixed conditions")


def weather_summary(code: int | None, temp_max_c: float | None, precip_prob_max: int | None) -> str:
    """Short line such as "Moderate rain, high 24°C, 85% chance of rain". Rendered from data only."""
    parts = [wmo_description(code)]
    if temp_max_c is not None:
        parts.append(f"high {round(temp_max_c)}°C")
    if precip_prob_max is not None:
        parts.append(f"{int(precip_prob_max)}% chance of rain")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")


def clean_text(value: str, max_len: int) -> str:
    """NFKC-normalize, drop control/bidi characters and markup, collapse whitespace, truncate."""
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"<[^>]{0,200}>", " ", text)
    text = _CONTROL.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item not in seen:
            seen[item] = None
    return list(seen)


# ---------------------------------------------------------------------------
# Local keyword search (fixture and snapshot providers)
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "a", "an", "the", "in", "near", "nearby", "around", "for", "with", "and", "or", "to", "of", "some",
    "place", "places", "spot", "spots", "good", "nice", "great", "best", "top", "something", "somewhere",
    "at", "on", "my", "me", "i", "we", "want", "find", "looking", "look", "thing", "things", "do",
    "activity", "activities", "fun", "stuff", "idea", "ideas", "local", "city", "area", "by", "that", "is",
    "are", "be", "can", "options", "option", "venue", "venues", "visit", "go", "out", "time", "saturday",
    "afternoon", "evening", "morning", "today", "weekend", "low", "key", "lowkey", "easy", "light", "proper",
    "really", "very", "bit", "little", "mood", "kind", "type", "style", "open", "now", "this", "these",
    "those", "it", "its", "like", "love", "loves", "enjoy", "just", "also", "maybe", "vibe", "vibes", "hangout",
    "hang", "cool", "cute", "quick", "short", "long", "spend", "while", "where", "which", "what", "should",
    "could", "would", "please", "any", "all", "more", "less", "other", "one", "two", "few", "after", "before",
    "then", "first", "last", "next", "from", "into", "up", "about", "our", "us", "your", "you", "they", "them",
    "who", "has", "have", "get", "lot", "lots", "pm", "am", "hour", "hours", "min", "minutes", "per", "person",
}

# Phrases collapsed to one token before tokenizing (applied to queries and candidate documents alike).
_PHRASES = [
    ("ice cream", "icecream"), ("live music", "livemusic"), ("open mic", "openmic"),
    ("street food", "streetfood"), ("fine dining", "finedining"), ("pure veg", "vegetarian"),
    ("plant based", "vegan"), ("plant-based", "vegan"), ("wheel chair", "wheelchair"),
    ("stand up", "standup"), ("stand-up", "standup"), ("art gallery", "gallery art"),
]

# query token -> (primary terms, related terms). Primary matches score 1.0, related 0.5.
_CONCEPTS: dict[str, tuple[set[str], set[str]]] = {
    "coffee": ({"coffee"}, {"cafe"}),
    "cafe": ({"cafe"}, {"coffee"}),
    "tea": ({"tea", "chai"}, {"cafe"}),
    "book": ({"book"}, {"library"}),
    "bookshop": ({"book"}, set()),
    "bookstore": ({"book"}, set()),
    "library": ({"library", "book"}, set()),
    "music": ({"music"}, {"livemusic", "jazz", "acoustic", "concert"}),
    "livemusic": ({"livemusic", "music"}, {"jazz", "acoustic"}),
    "concert": ({"concert", "music"}, set()),
    "gig": ({"music", "livemusic"}, set()),
    "jazz": ({"jazz"}, {"music", "livemusic"}),
    "acoustic": ({"acoustic"}, {"music"}),
    "openmic": ({"openmic"}, {"music", "comedy"}),
    "comedy": ({"comedy", "standup"}, {"openmic"}),
    "standup": ({"comedy", "standup"}, {"openmic"}),
    "walk": ({"walk"}, {"park", "garden", "lake", "nature", "heritage"}),
    "walking": ({"walk"}, {"park", "garden", "lake"}),
    "stroll": ({"walk"}, {"park", "garden", "lake"}),
    "park": ({"park"}, {"garden", "walk", "nature"}),
    "garden": ({"garden"}, {"park", "flower", "nature"}),
    "nature": ({"nature"}, {"park", "garden", "lake"}),
    "green": ({"nature", "park", "garden"}, set()),
    "lake": ({"lake"}, {"nature", "walk"}),
    "museum": ({"museum"}, {"history", "art", "science"}),
    "science": ({"science"}, {"museum"}),
    "history": ({"history", "heritage"}, {"museum"}),
    "heritage": ({"heritage", "history"}, {"museum"}),
    "historic": ({"heritage", "history"}, set()),
    "historical": ({"heritage", "history"}, set()),
    "art": ({"art"}, {"gallery", "museum"}),
    "arts": ({"art"}, {"gallery"}),
    "artsy": ({"art"}, {"gallery"}),
    "gallery": ({"gallery"}, {"art"}),
    "photography": ({"photography"}, {"art", "museum"}),
    "food": ({"food"}, {"restaurant", "cafe", "streetfood"}),
    "eat": ({"food"}, {"restaurant"}),
    "meal": ({"restaurant", "meal"}, {"food"}),
    "lunch": ({"restaurant", "lunch"}, {"food", "cafe"}),
    "dinner": ({"restaurant", "dinner"}, {"food", "finedining"}),
    "restaurant": ({"restaurant"}, {"food"}),
    "finedining": ({"finedining"}, {"restaurant"}),
    "brunch": ({"brunch"}, {"breakfast", "cafe"}),
    "breakfast": ({"breakfast"}, {"brunch", "cafe"}),
    "snack": ({"snack", "streetfood"}, {"food", "cafe"}),
    "streetfood": ({"streetfood"}, {"food", "chaat"}),
    "chaat": ({"chaat", "streetfood"}, {"food"}),
    "thali": ({"thali"}, {"restaurant"}),
    "dessert": ({"dessert"}, {"icecream", "sweet"}),
    "icecream": ({"icecream"}, {"dessert"}),
    "sweet": ({"dessert", "sweet"}, {"icecream"}),
    "bar": ({"bar"}, {"drink", "nightlife"}),
    "pub": ({"bar"}, {"drink", "brewery"}),
    "drink": ({"drink"}, {"bar", "brewery"}),
    "beer": ({"brewery", "drink"}, {"bar"}),
    "brewery": ({"brewery"}, {"drink", "bar"}),
    "nightlife": ({"nightlife"}, {"bar", "livemusic"}),
    "theatre": ({"theatre"}, {"culture"}),
    "theater": ({"theatre"}, {"culture"}),
    "play": ({"theatre"}, set()),
    "drama": ({"theatre"}, set()),
    "cinema": ({"cinema", "movie"}, set()),
    "movie": ({"cinema", "movie"}, set()),
    "film": ({"cinema", "movie"}, set()),
    "mall": ({"mall"}, {"shopping"}),
    "shopping": ({"shopping"}, {"mall", "market"}),
    "market": ({"market"}, {"shopping"}),
    "bazaar": ({"market"}, {"shopping"}),
    "temple": ({"temple", "worship"}, {"culture"}),
    "church": ({"church", "worship"}, {"culture"}),
    "culture": ({"culture"}, {"museum", "art", "history"}),
    "cultural": ({"culture"}, {"museum", "art"}),
    "view": ({"view", "viewpoint"}, set()),
    "viewpoint": ({"viewpoint", "view"}, set()),
    "sunset": ({"view", "viewpoint"}, set()),
    "spa": ({"spa"}, {"wellness"}),
    "wellness": ({"wellness", "spa"}, set()),
    "game": ({"game"}, {"arcade"}),
    "arcade": ({"game", "arcade"}, set()),
    "family": ({"family", "kid"}, set()),
    "kid": ({"kid", "family"}, set()),
    "children": ({"kid", "family"}, set()),
    "romantic": ({"romantic"}, {"finedining"}),
    "date": ({"romantic"}, set()),
    "event": ({"event"}, set()),
}

# Ranking-only modifiers: never filter, only reorder.
_QUIET = {"quiet", "calm", "peaceful", "uncrowded", "relaxed", "relaxing", "cosy", "cozy", "chill", "serene"}
_CHEAP = {"cheap", "budget", "affordable", "inexpensive", "free"}
_INDOOR = {"indoor", "indoors", "covered", "rainy", "rain"}
_OUTDOOR = {"outdoor", "outdoors", "openair", "outside"}
_LATE = {"late", "night", "latenight", "midnight"}
_SEATED = {"seated", "sit", "sitdown"}
_DIET = {"vegetarian": "vegetarian", "veg": "vegetarian", "veggie": "vegetarian", "vegan": "vegan", "jain": "jain"}
_ACCESS = {"wheelchair", "accessible", "stepfree"}
_MODIFIERS = _QUIET | _CHEAP | _INDOOR | _OUTDOOR | _LATE | _SEATED | set(_DIET) | _ACCESS


_STEM_EXCEPTIONS = {"cafés": "cafe", "cafes": "cafe", "café": "cafe", "movies": "movie", "cookies": "cookie",
                    "pies": "pie", "series": "series", "jazz": "jazz", "news": "news"}


def _stem(token: str) -> str:
    if token in _STEM_EXCEPTIONS:
        return _STEM_EXCEPTIONS[token]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "is")) and len(token) > 3:
        return token[:-1]
    return token


def _raw_tokens(text: str) -> list[str]:
    low = unicodedata.normalize("NFKC", text).lower().replace("_", " ").replace("é", "e")
    for phrase, repl in _PHRASES:
        low = low.replace(phrase, repl)
    return re.findall(r"[a-z0-9]+", low)


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in _raw_tokens(text)]


def _doc_terms(c: Candidate) -> tuple[set[str], set[str]]:
    """(descriptive terms from category, tags, neighbourhood and evidence; name terms) for a candidate."""
    name_terms = set(_tokens(c.name))
    terms: set[str] = set(_tokens(c.category))
    terms.add(_stem(c.category))
    for tag in c.tags:
        terms.update(_tokens(tag))
    if c.neighbourhood:
        terms.update(_tokens(c.neighbourhood))
    info = load_categories().get(c.category)
    if info is not None:
        terms.add(info.group)
        if info.meal:
            terms.add("meal")
    if c.live_music:
        terms.update({"music", "livemusic"})
    if c.kind == "event":
        terms.add("event")
    return terms, name_terms


def _concept_score(token: str, terms: set[str], name_terms: set[str]) -> float:
    """1.0 for a descriptive match (+0.25 if the name agrees), 0.75 for a name-only match, 0.5 related."""
    primary, related = _CONCEPTS.get(token, ({token}, set()))
    if primary & terms:
        return 1.0 + (0.25 if primary & name_terms else 0.0)
    if primary & name_terms:
        return 0.75
    if related & (terms | name_terms):
        return 0.5
    return 0.0


def _modifier_score(mods: set[str], c: Candidate) -> float:
    score = 0.0
    if mods & _QUIET:
        score += {"low": 1.0, "medium": 0.3, "high": -1.0}[c.popularity]
    if mods & _CHEAP:
        score += 1.0 if c.cost.max == 0 else 0.5 if c.cost.max <= price_level_cap(1, c.cost.currency) else 0.0
    if mods & _INDOOR and c.indoor is True:
        score += 0.75
    if mods & _OUTDOOR and c.indoor is False:
        score += 0.75
    if mods & _LATE:
        close = latest_close(c.hours)
        score += 1.0 if close is not None and close >= 23 * 60 else 0.0
    if mods & _SEATED and c.seated:
        score += 0.5
    for word, key in _DIET.items():
        if word in mods:
            ev = c.diet.get(key, Evidence.UNKNOWN)
            score += {Evidence.CONFIRMED: 1.5, Evidence.LIKELY: 0.25, Evidence.UNKNOWN: 0.0, Evidence.NO: -2.0}[ev]
    if mods & _ACCESS:
        score += 1.5 if c.wheelchair == Evidence.CONFIRMED else -0.5 if c.wheelchair == Evidence.NO else 0.0
    return score


def _take_neighbourhood(
    tokens: list[str], neighbourhoods: Mapping[str, tuple[float, float]]
) -> tuple[str, float, float, list[str]] | None:
    """Find the longest neighbourhood name inside ``tokens``; return it, its centroid and the tokens without it."""
    for name in sorted(neighbourhoods, key=len, reverse=True):
        hood = _raw_tokens(name)
        n = len(hood)
        for i in range(len(tokens) - n + 1):
            if tokens[i : i + n] == hood:
                lat, lng = neighbourhoods[name]
                return name, lat, lng, tokens[:i] + tokens[i + n :]
    return None


def neighbourhood_centre(
    name: str | None, location: Location, table: Mapping[str, tuple[float, float]] | None = None
) -> tuple[float, float] | None:
    """Centroid of a named neighbourhood inside ``location`` (within 40 km): the provider's own table first,
    then the shared gazetteer (data/gazetteer.yaml). None when unknown."""
    if not name or not name.strip():
        return None
    key = " ".join(_raw_tokens(name))
    for table_name, (lat, lng) in (table or {}).items():
        if " ".join(_raw_tokens(table_name)) == key and within_city(lat, lng, location):
            return lat, lng
    place = lookup_place(name)
    if place is not None and not place.is_city and within_city(place.lat, place.lng, location):
        return place.lat, place.lng
    return None


def search_local(
    candidates: Sequence[Candidate],
    query: PlaceQuery,
    ctx: WorldContext,
    neighbourhoods: Mapping[str, tuple[float, float]] | None = None,
) -> list[Candidate]:
    """Deterministic keyword search over local candidates (fixture and snapshot providers).

    1. Keep candidates within 40 km of ``ctx.location`` (city isolation).
    2. ``query.category`` filters by category key (or group name such as "food"); unknown values are
       treated as an extra intent word instead.
    3. Intent words are matched against name, category, tags and neighbourhood with a small synonym table.
       Without a category filter, a candidate must match at least one content word (if the intent has
       any). Words like "quiet", "cheap", "indoor", "vegetarian", "wheelchair", "late" only re-rank.
    4. Distance from (near_lat, near_lng), else from ``query.neighbourhood`` (provider table, then the
       gazetteer), else from a neighbourhood named in the intent (e.g. "in Indiranagar"), else from the city
       centre; must be within ``radius_m``. Candidates in the named neighbourhood get a small boost.
    5. ``open_at_min`` excludes candidates whose known hours are closed then (unknown hours are kept);
       ``max_price_level`` excludes candidates whose minimum cost is above that level's band.
    6. Sort by relevance desc, distance asc, id asc; return ``limit`` copies with ``city_id`` set.
    """
    loc = ctx.location
    cat_filter = categories_for_filter(query.category)
    intent = query.intent or ""
    if query.category and cat_filter is None:
        intent = f"{intent} {query.category}"

    centre_lat, centre_lng = loc.lat, loc.lng
    if query.near_lat is not None and query.near_lng is not None:
        centre_lat, centre_lng = query.near_lat, query.near_lng
    has_near = query.near_lat is not None and query.near_lng is not None
    raw = _raw_tokens(intent)
    hood: str | None = None
    if query.neighbourhood and query.neighbourhood.strip():
        hood = " ".join(_raw_tokens(query.neighbourhood))
        hood_tokens = hood.split()
        for i in range(len(raw) - len(hood_tokens) + 1):  # the name is a place, not a search word
            if hood_tokens and raw[i : i + len(hood_tokens)] == hood_tokens:
                raw = raw[:i] + raw[i + len(hood_tokens) :]
                break
        centre = neighbourhood_centre(query.neighbourhood, loc, neighbourhoods)
        if centre is not None and not has_near:
            centre_lat, centre_lng = centre
    if neighbourhoods:
        found = _take_neighbourhood(raw, neighbourhoods)
        if found:
            intent_hood, hood_lat, hood_lng, raw = found
            if hood is None:
                hood = intent_hood
                if not has_near:
                    centre_lat, centre_lng = hood_lat, hood_lng

    city_words = set(_raw_tokens(loc.name)) | {"bangalore", "bengaluru", "mumbai", "bombay", "blr"}
    words = [_stem(t) for t in raw if t not in _STOPWORDS and t not in city_words]
    mods = {t for t in words if t in _MODIFIERS}
    content = dedupe(t for t in words if t not in _MODIFIERS)

    cap: int | None = None
    if query.max_price_level is not None:
        cap = price_level_cap(query.max_price_level, location_currency(loc))

    pool: list[tuple[float, Candidate, set[str], set[str]]] = []
    for c in candidates:
        if not within_city(c.lat, c.lng, loc):
            continue
        if cat_filter is not None and c.category not in cat_filter:
            continue
        dist = haversine_m(centre_lat, centre_lng, c.lat, c.lng)
        if dist > query.radius_m:
            continue
        if query.open_at_min is not None and is_open_at(c.hours, query.open_at_min) is False:
            continue
        if cap is not None and c.cost.min > cap:
            continue
        terms, name_terms = _doc_terms(c)
        pool.append((dist, c, terms, name_terms))

    # Unknown words (not in the synonym table) that match nothing in the pool are treated as noise
    # ("rooftop cafe" still finds cafes) unless no content word is known ("sushi" finds nothing).
    known = [t for t in content if t in _CONCEPTS]
    unknown_hits = [t for t in content if t not in _CONCEPTS and any(t in row[2] or t in row[3] for row in pool)]
    if known or unknown_hits:
        content = known + unknown_hits

    scored: list[tuple[float, float, str, Candidate]] = []
    for dist, c, terms, name_terms in pool:
        content_score = sum(_concept_score(t, terms, name_terms) for t in content)
        if cat_filter is None and content and content_score == 0:
            continue
        score = content_score + _modifier_score(mods, c)
        if hood and c.neighbourhood and hood in " ".join(_raw_tokens(c.neighbourhood)):
            score += 0.5
        scored.append((-score, round(dist, 1), c.id, c))

    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3].model_copy(update={"city_id": loc.id}) for row in scored[: query.limit]]

