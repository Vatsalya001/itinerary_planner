"""Google Places API (New): Text Search for live candidates, Place Details for share-link re-hydration.

Terms (IMPLEMENTATION_PLAN.md section 6): only place IDs may be stored long-term; display fields are
re-fetched. Results are shown with "Google Maps" attribution and link to ``googleMapsUri``. The API key is
sent only in the ``X-Goog-Api-Key`` header and never logged or put into error messages.

Health: each provider instance keeps the outcome of its last live call in ``status`` (``unknown`` before the
first call, then ``ok``, ``auth_error`` or ``unavailable``), which ``/api/health`` reports. A rejected key
(401, 403, or 400 with reason ``API_KEY_INVALID``) is logged at ERROR once per process with the status code
only. Callers still fall back to the snapshot, so users get a plan and operators still see the misconfiguration.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, date, datetime
from typing import Any, Literal

import httpx

from app.domain.categories import category, load_categories
from app.domain.realworld import policy_for
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
    join_next_day,
    location_currency,
    maps_search_url,
    merge_periods,
    neighbourhood_centre,
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
CONNECT_TIMEOUT_S = 4.0
# Passed per request. A bare float would replace the shared client's Timeout(8.0, connect=4.0) for every phase,
# connect included, so the per-request value is a Timeout with its own connect limit.
TIMEOUT = httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
SEARCH_FIELDS = (
    "id", "displayName", "formattedAddress", "location", "types", "primaryType", "googleMapsUri", "businessStatus",
    "accessibilityOptions", "priceLevel", "priceRange", "rating", "userRatingCount", "regularOpeningHours",
    "currentOpeningHours", "servesVegetarianFood", "liveMusic", "outdoorSeating", "websiteUri",
)
SEARCH_FIELD_MASK = ",".join(f"places.{f}" for f in SEARCH_FIELDS)
# Place Details for share-link rehydration. Opening hours, rating, rating count and the price fields are billed at
# the Place Details Enterprise SKU, which costs more per call than the Pro SKU the identity and display fields
# alone need. Without them a re-opened Google stop shows unknown hours and no rating. Rehydration copies display
# fields only (store.redact._restore); a stop's stored cost, category and schedule are never replaced.
DETAILS_FIELDS = (
    "id", "displayName", "formattedAddress", "location", "types", "primaryType", "googleMapsUri", "businessStatus",
    "regularOpeningHours", "currentOpeningHours", "rating", "userRatingCount", "priceLevel", "priceRange", "websiteUri",
)
DETAILS_FIELD_MASK = ",".join(DETAILS_FIELDS)
# get() returns a place whose Google types map to none of our categories under this placeholder instead of
# dropping it (a dropped place reads "Place details unavailable"). Rehydration keeps the stored category.
DETAILS_PLACEHOLDER_CATEGORY = "event"
PlacesStatus = Literal["unknown", "ok", "auth_error", "unavailable"]
_auth_error_logged = False  # one ERROR line per process for a rejected key
_FOOD_GROUPS = {"food", "drinks"}
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{4,300}$")


# ---------------------------------------------------------------------------
# Opening hours
# ---------------------------------------------------------------------------


class MalformedPlace(ValueError):
    """A place object that does not match the documented Places API (New) shape; the place is skipped."""


def _int_field(point: dict[str, Any], key: str, lo: int, hi: int, default: int | None = None) -> int:
    value = point.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise MalformedPlace(f"bad {key}")
    return value


def _hm(point: dict[str, Any]) -> int:
    return _int_field(point, "hour", 0, 24, 0) * 60 + _int_field(point, "minute", 0, 59, 0)


def _point_date(point: dict[str, Any]) -> date | None:
    d = point.get("date")
    if d is None:
        return None
    if not isinstance(d, dict):
        raise MalformedPlace("bad date")
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedPlace("bad date") from exc


def _periods(block: Any) -> list[tuple[dict[str, Any], dict[str, Any] | None]] | None:
    """(open, close) points of an opening-hours block; None when absent; MalformedPlace when malformed."""
    if block is None:
        return None
    if not isinstance(block, dict):
        raise MalformedPlace("bad hours")
    periods = block.get("periods")
    if periods is None:
        return None
    if not isinstance(periods, list):
        raise MalformedPlace("bad periods")
    out: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for p in periods:
        if not isinstance(p, dict) or not isinstance(p.get("open"), dict):
            raise MalformedPlace("bad period")
        close = p.get("close")
        if close is not None and not isinstance(close, dict):
            raise MalformedPlace("bad close")
        for point in (p["open"], close):
            if point is not None:
                _int_field(point, "day", 0, 6)
                _hm(point)
                _point_date(point)
        out.append((p["open"], close))
    return out


def _current_hours(current: Any, day: date) -> Hours | None:
    """Hours for ``day`` from ``currentOpeningHours`` (dated periods, next seven days), or None when the
    data does not cover that date. Periods reaching midnight are joined with next-day periods at 00:00."""
    periods = _periods(current)
    if not periods:
        return None
    dated: list[tuple[date, dict[str, Any], dict[str, Any] | None]] = []
    for open_, close in periods:
        od = _point_date(open_)
        if od is None:
            return None  # undated current hours: fall back to regular hours
        dated.append((od, open_, close))
    first = min(d for d, _, _ in dated)
    last = max(d for d, _, _ in dated)
    if not first <= day <= last:
        return None
    today: list[tuple[int, int]] = []
    next_day: list[tuple[int, int]] = []
    for od, open_, close in dated:
        if close is None:
            if od <= day:
                today.append(ALWAYS_OPEN)
            continue
        cd = _point_date(close) or od
        start = (od - day).days * DAY + _hm(open_)
        end = (cd - day).days * DAY + _hm(close)
        if end <= start:
            continue
        if start == DAY:
            next_day.append((0, end - DAY))
        if end <= 0 or start >= DAY:
            continue  # another day's period
        today.append((max(0, start), end))
    return Hours(status="known", periods=merge_periods(join_next_day(today, next_day)))


def _weekly_spans(periods: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> list[tuple[int, int, int]]:
    """(open weekday, start, end) with end relative to the opening day's midnight (may exceed 1440)."""
    spans: list[tuple[int, int, int]] = []
    for open_, close in periods:
        if close is None:
            raise MalformedPlace("missing close")
        oday, cday = _int_field(open_, "day", 0, 6), _int_field(close, "day", 0, 6)
        start, end = _hm(open_), ((cday - oday) % 7) * DAY + _hm(close)
        if end <= start:
            end += DAY
        spans.append((oday, start, end))
    return spans


def _regular_hours(regular: Any, day: date) -> Hours:
    """Hours for ``day`` from ``regularOpeningHours`` (Google weekday 0 = Sunday ... 6 = Saturday).
    Overnight closes become minutes above 1440; a single period without a close means open 24/7; per-day
    periods that reach midnight are joined with the next day's 00:00 opening."""
    periods = _periods(regular)
    if not periods:
        return Hours(status="unknown")
    if any(close is None for _, close in periods):
        return Hours(status="known", periods=[ALWAYS_OPEN])
    spans = _weekly_spans(periods)
    gday = (day.weekday() + 1) % 7
    today = [(s, e) for d, s, e in spans if d == gday]
    today += [(0, e - DAY) for d, _s, e in spans if (d + 1) % 7 == gday and e > DAY]  # previous night spills over
    next_day = [(s, e) for d, s, e in spans if d == (gday + 1) % 7]
    return Hours(status="known", periods=merge_periods(join_next_day(today, next_day)))


def google_hours_for_date(regular: dict[str, Any] | None, current: dict[str, Any] | None, day: date) -> Hours:
    """Prefer ``currentOpeningHours`` when it covers ``day`` (it includes special days), else regular hours."""
    return _current_hours(current, day) or _regular_hours(regular, day)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _units(money: Any, *, upper: bool = False) -> int | None:
    """Whole units of a google.type.Money; None when absent; MalformedPlace when malformed or negative."""
    if money is None:
        return None
    if not isinstance(money, dict):
        raise MalformedPlace("bad money")
    raw = money.get("units", "0")
    if isinstance(raw, bool) or not isinstance(raw, str | int) or not re.fullmatch(r"\d{1,9}", str(raw)):
        raise MalformedPlace("bad money units")
    nanos = money.get("nanos", 0)
    if isinstance(nanos, bool) or not isinstance(nanos, int) or not 0 <= nanos < 1_000_000_000:
        raise MalformedPlace("bad money nanos")
    return int(raw) + (1 if upper and nanos else 0)


def _cost(place: dict[str, Any], key: str, currency: str) -> CostRange:
    """Listed ``priceRange`` when it is in the location currency (any currency, not only INR), else the
    ``priceLevel`` band, else the category default."""
    pr = place.get("priceRange")
    if pr is not None and not isinstance(pr, dict):
        raise MalformedPlace("bad priceRange")
    if isinstance(pr, dict):
        start, end = pr.get("startPrice"), pr.get("endPrice")
        lo, hi = _units(start), _units(end, upper=True)
        cur = start.get("currencyCode") if isinstance(start, dict) else None
        if lo is not None and cur == currency:
            if hi is None or not isinstance(end, dict) or end.get("currencyCode") != currency:
                # open-ended range ("2000+"): use the band or category upper bound, never below lo. A bound from
                # another currency (no band table for this one) is never mixed in; the range is then unusable.
                fallback = price_level_band(place.get("priceLevel"), currency) or category_default_cost(key, currency)
                hi = fallback.max if fallback.max > lo else math.ceil(lo * 1.5) if fallback.currency == currency else None
            if hi is not None:
                if hi < lo:
                    raise MalformedPlace("inverted priceRange")
                return CostRange(min=lo, max=hi, currency=currency, basis="listed_price_range")
    band = price_level_band(place.get("priceLevel"), currency)
    if band is not None and band.currency == currency:
        return band
    estimated = category_default_cost(key, currency)
    # A zero category default is not evidence of free admission at this particular venue.
    return estimated.model_copy(update={"basis": "category_estimate"})


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
    if not found and wanted in {"museum", "art_gallery", "heritage_walk"}:
        return None  # a search category is not evidence that a tour agency is a historical attraction
    if not found and wanted in allowed:
        return wanted  # nothing maps (e.g. only "point_of_interest"): trust the requested category
    return None


_OPTIONAL_TYPES: dict[str, tuple[type, ...]] = {
    "primaryType": (str,), "formattedAddress": (str,), "googleMapsUri": (str,), "businessStatus": (str,),
    "priceLevel": (str,), "rating": (int, float), "userRatingCount": (int,), "servesVegetarianFood": (bool,),
    "liveMusic": (bool,), "outdoorSeating": (bool,), "accessibilityOptions": (dict,), "displayName": (dict,),
    "location": (dict,),
}


def _check_shape(place: dict[str, Any]) -> None:
    """Raise MalformedPlace when a documented field has the wrong type (missing fields are fine)."""
    for key, kinds in _OPTIONAL_TYPES.items():
        value = place.get(key)
        if value is None:
            continue
        if not isinstance(value, kinds) or (kinds != (bool,) and isinstance(value, bool)):
            raise MalformedPlace(f"bad {key}")
    types = place.get("types")
    if types is not None and (not isinstance(types, list) or not all(isinstance(t, str) for t in types)):
        raise MalformedPlace("bad types")
    count = place.get("userRatingCount")
    if count is not None and count < 0:
        raise MalformedPlace("negative userRatingCount")
    opts = place.get("accessibilityOptions")
    if isinstance(opts, dict) and opts.get("wheelchairAccessibleEntrance") not in (None, True, False):
        raise MalformedPlace("bad wheelchairAccessibleEntrance")


def place_to_candidate(
    place: dict[str, Any], ctx: WorldContext, wanted_category: str | None = None, now: datetime | None = None,
    *, unmapped_category: str | None = None,
) -> Candidate | None:
    """One Places API (New) place -> Candidate, or None (not operational, no location, no category,
    or more than 40 km from the city centre). Raises MalformedPlace for wrongly typed fields.

    ``unmapped_category`` (details lookups only) is used when the place's types map to no category, so the
    place is returned rather than dropped."""
    if not isinstance(place, dict):
        raise MalformedPlace("place is not an object")
    _check_shape(place)
    pid = place.get("id")
    if not isinstance(pid, str) or not pid:
        return None
    status = place.get("businessStatus")
    if status is not None and status != "OPERATIONAL":
        return None
    loc = place.get("location") or {}
    lat_raw, lng_raw = loc.get("latitude"), loc.get("longitude")
    if not isinstance(lat_raw, int | float) or not isinstance(lng_raw, int | float):
        return None
    if isinstance(lat_raw, bool) or isinstance(lng_raw, bool):
        return None
    lat, lng = float(lat_raw), float(lng_raw)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if not within_city(lat, lng, ctx.location):
        return None
    key = _choose_category(place, wanted_category) or unmapped_category
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
    if {"tour_agency", "travel_agency", "tourist_information_center"} & set(types):
        tags.append("listing:tour_operator")
    if live is True:
        tags.append("live music")
    if place.get("outdoorSeating") is True:
        tags.append("outdoor seating")
    maps_uri = place.get("googleMapsUri")
    rating = place.get("rating")
    candidate = Candidate(
        website_url=place.get("websiteUri") if isinstance(place.get("websiteUri"), str) and len(place["websiteUri"]) <= 2048 else None,
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


    policy = policy_for(candidate, ctx)
    if policy and policy.get("general_admission_free") and key in {"museum", "art_gallery"}:
        candidate = candidate.model_copy(update={
            "cost": CostRange(min=0, max=0, currency=currency, basis="free"),
            "tags": [*candidate.tags, "admission:general_free"],
        })
    return candidate


def text_query(intent: str, location: Location, neighbourhood: str | None = None) -> str:
    """``"{intent} in {neighbourhood}, {city}"`` or ``"{intent} in {city}"``; an intent that already names a
    place ("cafe in Indiranagar") gets the city appended after a comma instead."""
    intent = re.sub(r"\s+", " ", intent).strip()
    city = location.name
    hood = re.sub(r"\s+", " ", neighbourhood or "").strip()
    if hood and hood.lower() not in intent.lower():
        return f"{intent} in {hood}, {city}"
    if city.lower() in intent.lower():
        return intent
    if re.search(r"\b(in|near|around|at)\s+\w", intent, flags=re.IGNORECASE) or hood:
        return f"{intent}, {city}"
    return f"{intent} in {city}"


def _is_key_rejected(resp: httpx.Response) -> bool:
    """401/403, or Google's 400 for an invalid key (``error.details[].reason == "API_KEY_INVALID"``)."""
    if resp.status_code in (401, 403):
        return True
    if resp.status_code != 400:
        return False
    try:
        error = resp.json().get("error")
        details = error.get("details") if isinstance(error, dict) else None
    except Exception:  # an unreadable body is not evidence of a key problem
        return False
    return isinstance(details, list) and any(
        isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details)


def _log_rejected_key(status_code: int) -> None:
    global _auth_error_logged
    if _auth_error_logged:
        log.warning("google places HTTP %s (API key rejected)", status_code)
        return
    _auth_error_logged = True
    log.error("google places rejected the API key (HTTP %s); live place data is off and searches fall back to "
              "the snapshot until GOOGLE_MAPS_API_KEY is fixed", status_code,
              extra={"status_code": status_code, "provider": PROVIDER})


class GooglePlaces:
    """PlacesProvider over Google Places API (New). ``covers`` is always True (global coverage).

    ``status`` is the outcome of the last live call (``unknown`` before the first), reported by /api/health."""

    name = "google_places"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._client = client
        self.status: PlacesStatus = "unknown"

    def __repr__(self) -> str:  # never expose the key
        return "GooglePlaces()"

    def covers(self, location: Location) -> bool:
        return True

    async def _call(self, method: str, url: str, field_mask: str, json_body: dict[str, Any] | None = None) -> Any:
        headers = {"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": field_mask}
        try:
            if method == "POST":
                resp = await self._client.post(url, json=json_body, headers=headers, timeout=TIMEOUT)
            else:
                resp = await self._client.get(url, headers=headers, timeout=TIMEOUT)
        except httpx.TimeoutException as exc:
            self.status = "unavailable"
            raise ProviderError(PROVIDER, "timeout", "request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            self.status = "unavailable"
            raise ProviderError(PROVIDER, "network", type(exc).__name__, retryable=True) from exc
        if resp.status_code == 404 and method == "GET":
            self.status = "ok"
            return None
        if resp.status_code != 200:
            code = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                    429: "rate_limited"}.get(resp.status_code, f"http_{resp.status_code}")
            if _is_key_rejected(resp):
                self.status = "auth_error"
                _log_rejected_key(resp.status_code)
            else:
                self.status = "unavailable"
                log.warning("google places HTTP %s", resp.status_code)
            raise ProviderError(PROVIDER, code, f"HTTP {resp.status_code}",
                                retryable=resp.status_code == 429 or resp.status_code >= 500)
        try:
            data = resp.json()
        except ValueError as exc:
            self.status = "unavailable"
            raise ProviderError(PROVIDER, "bad_response", "invalid JSON") from exc
        self.status = "ok"
        return data

    async def search(self, query: PlaceQuery, ctx: WorldContext) -> list[Candidate]:
        lat, lng = ctx.location.lat, ctx.location.lng
        if query.near_lat is not None and query.near_lng is not None:
            lat, lng = query.near_lat, query.near_lng
        elif query.neighbourhood:
            centre = neighbourhood_centre(query.neighbourhood, ctx.location)
            if centre is not None:
                lat, lng = centre
        intent = query.intent.strip() or (query.category or "things to do").replace("_", " ")
        body: dict[str, Any] = {
            "textQuery": text_query(intent, ctx.location, query.neighbourhood),
            "pageSize": min(query.limit, 10),
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(query.radius_m)}},
            "languageCode": "en",
            "regionCode": ctx.location.country_code,
        }
        data = await self._call("POST", SEARCH_URL, SEARCH_FIELD_MASK, body)
        if not isinstance(data, dict):
            raise ProviderError(PROVIDER, "bad_response", "unexpected response shape")
        places = data.get("places", [])
        if not isinstance(places, list):
            raise ProviderError(PROVIDER, "bad_response", "unexpected places field")
        now = datetime.now(UTC)
        currency = location_currency(ctx.location)
        out: list[Candidate] = []
        for index, place in enumerate(places):
            try:
                cand = place_to_candidate(place, ctx, query.category, now)
            except Exception:  # one malformed place must never sink the whole search
                log.warning("google_places: skipped malformed place at index %d", index)
                continue
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
        """Place Details for share-link rehydration (display fields, hours and rating). A place whose types map
        to no category is still returned (``DETAILS_PLACEHOLDER_CATEGORY``); the caller keeps its own category."""
        if not candidate_id.startswith("gp:"):
            return None
        pid = candidate_id[3:]
        if not _PLACE_ID.match(pid):
            return None
        data = await self._call("GET", DETAILS_URL.format(place_id=pid), DETAILS_FIELD_MASK)
        if data is None:
            return None
        try:
            return place_to_candidate(data, ctx, unmapped_category=DETAILS_PLACEHOLDER_CATEGORY)
        except Exception as exc:
            raise ProviderError(PROVIDER, "bad_response", "malformed place details") from exc
