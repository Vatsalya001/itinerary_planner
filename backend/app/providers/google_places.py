"""Google Places API (New): Text Search for live candidates, Place Details (Pro mask) for re-hydration.

Terms (IMPLEMENTATION_PLAN.md section 6): only place IDs may be stored long-term; display fields are
re-fetched. Results are shown with "Google Maps" attribution and link to ``googleMapsUri``. The API key is
sent only in the ``X-Goog-Api-Key`` header and never logged or put into error messages.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.domain.categories import category, load_categories
from app.domain.schemas import (
    Candidate,
    CostRange,
    Evidence,
    Hours,
    Location,
    PlaceQuery,
    Source,
    WorldContext,
)
from app.providers.base import ProviderError
from app.providers.normalize import (
    ALWAYS_OPEN,
    DAY,
    busy_windows_for,
    categories_for_filter,
    category_default_cost,
    clean_text,
    dedupe,
    google_types_to_categories,
    is_https_url,
    is_open_at,
    location_currency,
    maps_search_url,
    merge_periods,
    popularity_from_rating_count,
    price_level_band,
    price_level_cap,
    price_level_index,
    within_city,
)

log = logging.getLogger(__name__)

PROVIDER = "google_places"
SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
TIMEOUT_S = 8.0
SEARCH_FIELDS = (
    "id", "displayName", "formattedAddress", "location", "types", "primaryType", "googleMapsUri", "businessStatus",
    "accessibilityOptions", "priceLevel", "priceRange", "rating", "userRatingCount", "regularOpeningHours",
    "currentOpeningHours", "servesVegetarianFood", "liveMusic", "outdoorSeating",
)
SEARCH_FIELD_MASK = ",".join(f"places.{f}" for f in SEARCH_FIELDS)
DETAILS_FIELD_MASK = "id,displayName,formattedAddress,location,types,googleMapsUri,businessStatus"  # Pro SKU
_FOOD_GROUPS = {"food", "drinks"}
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{4,300}$")


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------


def _hm(point: dict[str, Any]) -> int:
    return int(point.get("hour", 0)) * 60 + int(point.get("minute", 0))


def _point_date(point: dict[str, Any]) -> date | None:
    d = point.get("date")
    if not isinstance(d, dict):
        return None
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _current_hours(current: dict[str, Any] | None, day: date) -> Hours | None:
    """Hours for ``day`` from ``currentOpeningHours`` (dated periods, next seven days), or None when the
    data does not cover that date."""
    if not isinstance(current, dict):
        return None
    periods = current.get("periods")
    if not isinstance(periods, list) or not periods:
        return None
    dated: list[tuple[date, dict[str, Any], dict[str, Any] | None]] = []
    for p in periods:
        if not isinstance(p, dict) or not isinstance(p.get("open"), dict):
            continue
        od = _point_date(p["open"])
        if od is None:
            return None  # undated current hours: fall back to regular hours
        close = p.get("close") if isinstance(p.get("close"), dict) else None
        dated.append((od, p["open"], close))
    if not dated:
        return None
    first = min(d for d, _, _ in dated)
    last = max(d for d, _, _ in dated)
    if not first <= day <= last:
        return None
    out: list[tuple[int, int]] = []
    for od, open_, close in dated:
        if close is None:
            if od <= day:
                out.append(ALWAYS_OPEN)
            continue
        cd = _point_date(close) or od
        start = (od - day).days * DAY + _hm(open_)
        end = (cd - day).days * DAY + _hm(close)
        if end <= 0 or start >= DAY:
            continue  # another day's period
        out.append((max(0, start), end))
    return Hours(status="known", periods=merge_periods(out))


def _regular_hours(regular: dict[str, Any] | None, day: date) -> Hours:
    """Hours for ``day`` from ``regularOpeningHours`` (Google weekday 0 = Sunday ... 6 = Saturday).
    Overnight closes become minutes above 1440; a single period without a close means open 24/7."""
    if not isinstance(regular, dict):
        return Hours(status="unknown")
    periods = regular.get("periods")
    if not isinstance(periods, list) or not periods:
        return Hours(status="unknown")
    gday = (day.weekday() + 1) % 7
    out: list[tuple[int, int]] = []
    for p in periods:
        if not isinstance(p, dict) or not isinstance(p.get("open"), dict):
            continue
        open_ = p["open"]
        close = p.get("close") if isinstance(p.get("close"), dict) else None
        if close is None:
            return Hours(status="known", periods=[ALWAYS_OPEN])
        oday, cday = int(open_.get("day", -1)), int(close.get("day", -1))
        span_days = (cday - oday) % 7
        start, end = _hm(open_), span_days * DAY + _hm(close)
        if end <= start:
            end += DAY
        if oday == gday:
            out.append((start, end))
        elif (oday + 1) % 7 == gday and end > DAY:
            out.append((0, end - DAY))  # previous day's overnight period spilling into this day
    return Hours(status="known", periods=merge_periods(out))


def google_hours_for_date(regular: dict[str, Any] | None, current: dict[str, Any] | None, day: date) -> Hours:
    """Prefer ``currentOpeningHours`` when it covers ``day`` (it includes special days), else regular hours."""
    return _current_hours(current, day) or _regular_hours(regular, day)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _units(money: Any) -> int | None:
    if not isinstance(money, dict):
        return None
    try:
        return int(str(money.get("units", "0")))
    except ValueError:
        return None


def _cost(place: dict[str, Any], key: str, currency: str) -> CostRange:
    pr = place.get("priceRange")
    if isinstance(pr, dict):
        start, end = pr.get("startPrice"), pr.get("endPrice")
        cur = start.get("currencyCode") if isinstance(start, dict) else None
        lo = _units(start)
        if lo is not None and cur == currency:
            hi = _units(end) if isinstance(end, dict) and end.get("currencyCode") == currency else None
            if hi is None:  # open-ended range ("2000+"): use the band or category upper bound, never below lo
                fallback = price_level_band(place.get("priceLevel"), currency) or category_default_cost(key, currency)
                hi = max(lo, fallback.max)
            return CostRange(min=max(0, lo), max=max(lo, hi), currency=currency, basis="listed_price_range")
    band = price_level_band(place.get("priceLevel"), currency)
    if band is not None and band.currency == currency:
        return band
    return category_default_cost(key, currency)


def _diet(place: dict[str, Any], types: list[str]) -> dict[str, Evidence]:
    serves = place.get("servesVegetarianFood")
    veg = Evidence.CONFIRMED if serves is True else Evidence.NO if serves is False else Evidence.UNKNOWN
    vegan = Evidence.UNKNOWN
    if "vegetarian_restaurant" in types:
        veg = Evidence.CONFIRMED
    if "vegan_restaurant" in types:
        vegan = Evidence.CONFIRMED
        veg = Evidence.CONFIRMED
    return {"vegetarian": veg, "vegan": vegan, "jain": Evidence.UNKNOWN}


def _wheelchair(place: dict[str, Any]) -> Evidence:
    opts = place.get("accessibilityOptions")
    value = opts.get("wheelchairAccessibleEntrance") if isinstance(opts, dict) else None
    return Evidence.CONFIRMED if value is True else Evidence.NO if value is False else Evidence.UNKNOWN


def _choose_category(place: dict[str, Any], wanted: str | None) -> str | None:
    found = google_types_to_categories(place.get("primaryType"), place.get("types") or [])
    allowed = categories_for_filter(wanted)
    if allowed is None:
        if found:
            return found[0]
        return wanted if wanted and wanted in load_categories() else None
    for key in found:
        if key in allowed:
            return key
    if not found and wanted in allowed:
        return wanted  # nothing maps (e.g. only "point_of_interest"): trust the requested category
    return None


def place_to_candidate(
    place: dict[str, Any], ctx: WorldContext, wanted_category: str | None = None, now: datetime | None = None
) -> Candidate | None:
    """One Places API (New) place -> Candidate, or None (not operational, no location, no category,
    or more than 40 km from the city centre)."""
    pid = place.get("id")
    if not isinstance(pid, str) or not pid:
        return None
    status = place.get("businessStatus")
    if status is not None and status != "OPERATIONAL":
        return None
    loc = place.get("location") or {}
    try:
        lat, lng = float(loc["latitude"]), float(loc["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not within_city(lat, lng, ctx.location):
        return None
    key = _choose_category(place, wanted_category)
    if key is None:
        return None
    info = category(key)
    types = [t for t in (place.get("types") or []) if isinstance(t, str)]
    name_obj = place.get("displayName")
    raw_name = name_obj.get("text") if isinstance(name_obj, dict) else None
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None
    name = clean_text(raw_name, 120)
    currency = location_currency(ctx.location)
    count = place.get("userRatingCount")
    rating_count = int(count) if isinstance(count, int | float) else None
    popularity = popularity_from_rating_count(rating_count)
    live = place.get("liveMusic")
    tags = list(info.tags)
    if live is True:
        tags.append("live music")
    if place.get("outdoorSeating") is True:
        tags.append("outdoor seating")
    maps_uri = place.get("googleMapsUri")
    rating = place.get("rating")
    return Candidate(
        id=f"gp:{pid}",
        kind="place",
        name=name,
        category=key,
        city_id=ctx.location.id,
        lat=lat,
        lng=lng,
        address=clean_text(place["formattedAddress"], 200) if isinstance(place.get("formattedAddress"), str) else None,
        cost=_cost(place, key, currency),
        rating=float(rating) if isinstance(rating, int | float) else None,
        rating_count=rating_count,
        hours=google_hours_for_date(place.get("regularOpeningHours"), place.get("currentOpeningHours"), ctx.date),
        diet=_diet(place, types) if info.group in _FOOD_GROUPS else {},
        wheelchair=_wheelchair(place),
        indoor=info.indoor,
        seated=info.seated,
        effort=info.effort,
        serves_alcohol=True if info.alcohol else None,
        live_music=live if isinstance(live, bool) else None,
        popularity=popularity,
        busy_windows=busy_windows_for(key, popularity),
        tags=dedupe(tags),
        duration_range=info.duration,
        maps_url=maps_uri if is_https_url(maps_uri) else maps_search_url(name, lat, lng, pid),
        source=Source(provider="google_places", url=maps_uri if is_https_url(maps_uri) else None,
                      retrieved_at=now or datetime.now(UTC), attribution="Google Maps"),
    )


def text_query(intent: str, location: Location) -> str:
    """``"{intent} in {city}"``; an intent that already names a place ("cafe in Indiranagar") gets the city
    appended after a comma instead."""
    intent = re.sub(r"\s+", " ", intent).strip()
    city = location.name
    if city.lower() in intent.lower():
        return intent
    if re.search(r"\b(in|near|around|at)\s+\w", intent, flags=re.IGNORECASE):
        return f"{intent}, {city}"
    return f"{intent} in {city}"


class GooglePlaces:
    """PlacesProvider over Google Places API (New). ``covers`` is always True (global coverage)."""

    name = "google_places"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._client = client

    def __repr__(self) -> str:  # never expose the key
        return "GooglePlaces()"

    def covers(self, location: Location) -> bool:
        return True

    async def _call(self, method: str, url: str, field_mask: str, json_body: dict[str, Any] | None = None) -> Any:
        headers = {"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": field_mask}
        try:
            if method == "POST":
                resp = await self._client.post(url, json=json_body, headers=headers, timeout=TIMEOUT_S)
            else:
                resp = await self._client.get(url, headers=headers, timeout=TIMEOUT_S)
        except httpx.TimeoutException as exc:
            raise ProviderError(PROVIDER, "timeout", "request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER, "network", type(exc).__name__, retryable=True) from exc
        if resp.status_code == 404 and method == "GET":
            return None
        if resp.status_code != 200:
            code = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                    429: "rate_limited"}.get(resp.status_code, f"http_{resp.status_code}")
            log.warning("google places HTTP %s", resp.status_code)
            raise ProviderError(PROVIDER, code, f"HTTP {resp.status_code}",
                                retryable=resp.status_code == 429 or resp.status_code >= 500)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(PROVIDER, "bad_response", "invalid JSON") from exc

    async def search(self, query: PlaceQuery, ctx: WorldContext) -> list[Candidate]:
        lat = query.near_lat if query.near_lat is not None else ctx.location.lat
        lng = query.near_lng if query.near_lng is not None else ctx.location.lng
        intent = query.intent.strip() or (query.category or "things to do").replace("_", " ")
        body: dict[str, Any] = {
            "textQuery": text_query(intent, ctx.location),
            "pageSize": min(query.limit, 10),
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(query.radius_m)}},
            "languageCode": "en",
            "regionCode": ctx.location.country_code,
        }
        data = await self._call("POST", SEARCH_URL, SEARCH_FIELD_MASK, body)
        places = data.get("places") if isinstance(data, dict) else None
        now = datetime.now(UTC)
        currency = location_currency(ctx.location)
        out: list[Candidate] = []
        for place in places or []:
            if not isinstance(place, dict):
                continue
            cand = place_to_candidate(place, ctx, query.category, now)
            if cand is None:
                continue
            if query.open_at_min is not None and is_open_at(cand.hours, query.open_at_min) is False:
                continue
            if query.max_price_level is not None:
                level = price_level_index(place.get("priceLevel"))
                if level is not None and level > query.max_price_level:
                    continue
                if level is None and cand.cost.min > price_level_cap(query.max_price_level, currency):
                    continue
            out.append(cand)
            if len(out) >= query.limit:
                break
        return out

    async def get(self, candidate_id: str, ctx: WorldContext) -> Candidate | None:
        if not candidate_id.startswith("gp:"):
            return None
        pid = candidate_id[3:]
        if not _PLACE_ID.match(pid):
            return None
        data = await self._call("GET", DETAILS_URL.format(place_id=pid), DETAILS_FIELD_MASK)
        if not isinstance(data, dict):
            return None
        return place_to_candidate(data, ctx)

