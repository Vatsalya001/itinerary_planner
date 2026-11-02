"""Open-Meteo geocoding and daily forecast (no key; free tier is non-commercial, see SOURCES.md)."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime
from functools import lru_cache
from typing import Any

import httpx
import yaml

from app.domain.categories import DATA_DIR, currency_for_country, load_countries
from app.domain.parser_rules import GazPlace, lookup_place
from app.domain.schemas import Location, Weather
from app.providers.base import ProviderError
from app.providers.normalize import haversine_m, weather_summary

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 4.0
# Per-request Timeout with its own connect limit (a bare float would also set connect to 8 s).
TIMEOUT = httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
DAILY_FIELDS = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"

# Old or colloquial names -> the name GeoNames knows. Tried only when the first query returns nothing.
CITY_ALIASES: dict[str, str] = {
    "bangalore": "Bengaluru", "blr": "Bengaluru", "bombay": "Mumbai", "madras": "Chennai", "calcutta": "Kolkata",
    "gurgaon": "Gurugram", "poona": "Pune", "mysore": "Mysuru", "trivandrum": "Thiruvananthapuram",
    "pondicherry": "Puducherry", "cochin": "Kochi", "baroda": "Vadodara", "benares": "Varanasi",
    "banaras": "Varanasi", "mangalore": "Mangaluru", "calicut": "Kozhikode", "vizag": "Visakhapatnam",
    "allahabad": "Prayagraj", "simla": "Shimla", "belgaum": "Belagavi", "hubli": "Hubballi", "nyc": "New York",
    "new york city": "New York", "sf": "San Francisco",
}


GAZETTEER_MATCH_KM = 50.0  # a geocoder result this close to the gazetteer centre is the same city
MAX_GAZETTEER_QUERIES = 3

# Country words a user may add after a comma ("Bangalore, India"). Two-letter codes are accepted as-is.
COUNTRY_WORDS: dict[str, str] = {
    "india": "IN", "uk": "GB", "united kingdom": "GB", "england": "GB", "britain": "GB", "great britain": "GB",
    "scotland": "GB", "wales": "GB", "usa": "US", "united states": "US", "america": "US", "uae": "AE",
    "united arab emirates": "AE", "singapore": "SG", "germany": "DE", "france": "FR", "netherlands": "NL",
    "holland": "NL", "spain": "ES", "italy": "IT", "japan": "JP", "australia": "AU", "canada": "CA",
    "ireland": "IE", "portugal": "PT", "austria": "AT", "czechia": "CZ", "czech republic": "CZ",
    "switzerland": "CH", "denmark": "DK", "sweden": "SE", "turkey": "TR", "mexico": "MX", "new zealand": "NZ",
    "qatar": "QA",
}


def _fold(text: str) -> str:
    """Case- and accent-insensitive comparison key."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", plain.lower())).strip()


def _country_code(hint: str) -> str | None:
    key = _fold(hint)
    if not key:
        return None
    if len(key) == 2 and key.isalpha():
        return key.upper()
    return COUNTRY_WORDS.get(key) or (key.upper() if key.upper() in load_countries() else None)


@lru_cache(maxsize=1)
def _gazetteer_aliases() -> dict[str, list[str]]:
    """Canonical gazetteer city -> its aliases (data/gazetteer.yaml), for geocoder queries."""
    raw: dict[str, Any] = yaml.safe_load((DATA_DIR / "gazetteer.yaml").read_text(encoding="utf-8")) or {}
    return {str(name): [str(a) for a in (info.get("aliases") or [])] for name, info in (raw.get("cities") or {}).items()}


def gazetteer_city(name: str) -> GazPlace | None:
    """The gazetteer city for a city name or alias; a neighbourhood resolves to its city. None if unknown."""
    place = lookup_place(name)
    if place is None:
        return None
    if not place.is_city:
        parent = lookup_place(place.city)
        return parent if parent is not None and parent.is_city else None
    return place


def _query_names(typed: str, place: GazPlace) -> list[str]:
    """Names to send to the geocoder, in gazetteer spelling: the one the user typed first, then the canonical
    name, then other aliases. All-caps acronyms (BLR, NYC) are never sent: they match unrelated places."""
    known = [place.name, *_gazetteer_aliases().get(place.name, [])]
    typed_key = _fold(typed)
    known.sort(key=lambda n: _fold(n) != typed_key)  # stable: the typed spelling first
    out: list[str] = []
    seen: set[str] = set()
    for name in known:
        name = name.strip()
        if len(name) < 3 or (name.isupper() and len(name) <= 5) or _fold(name) in seen:
            continue
        seen.add(_fold(name))
        out.append(name)
    return out


def location_from_gazetteer(place: GazPlace) -> Location:
    slug = re.sub(r"[^a-z0-9]+", "-", _fold(place.name)).strip("-")
    return Location(
        id=f"gazetteer:{slug}",
        name=place.name,
        country_code=place.country_code,
        lat=place.lat,
        lng=place.lng,
        timezone=place.timezone,
        currency=currency_for_country(place.country_code),
    )


def rank_locations(results: list[Location], names: list[str]) -> list[Location]:
    """Exact (case/accent-insensitive) name matches first, then larger population; stable otherwise."""
    wanted = {_fold(n) for n in names}
    return sorted(results, key=lambda r: (_fold(r.name) not in wanted, -(r.population or 0)))


def _unavailable() -> Weather:
    return Weather(status="unavailable", summary="Weather not available")


def _status_error(provider: str, resp: httpx.Response) -> ProviderError:
    code = "rate_limited" if resp.status_code == 429 else f"http_{resp.status_code}"
    return ProviderError(provider, code, f"HTTP {resp.status_code}",
                         retryable=resp.status_code == 429 or resp.status_code >= 500)


def _location_from(result: dict[str, Any]) -> Location | None:
    tz, name, cc_raw = result.get("timezone"), result.get("name"), result.get("country_code")
    if not (isinstance(tz, str) and tz and isinstance(name, str) and name and isinstance(cc_raw, str) and cc_raw):
        return None
    try:
        cc = cc_raw.upper()
        return Location(
            id=f"geonames:{result['id']}",
            name=name,
            country_code=cc,
            admin1=result.get("admin1"),
            lat=float(result["latitude"]),
            lng=float(result["longitude"]),
            timezone=tz,
            currency=currency_for_country(cc),
            population=result.get("population"),
        )
    except (KeyError, TypeError, ValueError):
        return None


class OpenMeteoGeocoder:
    """Geocoder over the Open-Meteo geocoding API (GeoNames data). Raises ProviderError on HTTP failure."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _search(self, name: str, country_code: str | None = None) -> list[Location]:
        params: dict[str, str | int] = {"name": name, "count": 10, "language": "en", "format": "json"}
        if country_code:
            params["countryCode"] = country_code
        try:
            resp = await self._client.get(GEOCODE_URL, params=params, timeout=TIMEOUT)
        except httpx.TimeoutException as exc:
            raise ProviderError("open_meteo_geocoding", "timeout", "geocoding timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("open_meteo_geocoding", "network", type(exc).__name__, retryable=True) from exc
        if resp.status_code != 200:
            raise _status_error("open_meteo_geocoding", resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("open_meteo_geocoding", "bad_response", "invalid JSON") from exc
        results = data.get("results") if isinstance(data, dict) else "not an object"
        if results is None:
            results = []  # Open-Meteo omits "results" when nothing matches
        if not isinstance(results, list):
            raise ProviderError("open_meteo_geocoding", "bad_response", "unexpected response shape")
        out: list[Location] = []
        for result in results:
            try:
                loc = _location_from(result) if isinstance(result, dict) else None
            except Exception:  # one odd result is skipped, never a crash
                loc = None
            if loc is not None:
                out.append(loc)
        return out

    async def _resolve_known(self, typed: str, place: GazPlace) -> list[Location]:
        """A gazetteer city: ask Open-Meteo in that country under each known spelling and accept a result
        only if it carries one of those names and is the same city (same country, within 50 km of the
        gazetteer centre); other nearby places ("Dharavi" for "Bombay") are kept only as runners-up.
        Falls back to a Location built from the gazetteer when no spelling matches or Open-Meteo fails."""
        names = _query_names(typed, place)
        wanted = {_fold(n) for n in names}
        try:
            for name in names[:MAX_GAZETTEER_QUERIES]:
                results = await self._search(name, place.country_code)
                same_city = [
                    r for r in results
                    if r.country_code == place.country_code
                    and haversine_m(r.lat, r.lng, place.lat, place.lng) <= GAZETTEER_MATCH_KM * 1000
                ]
                if any(_fold(r.name) in wanted for r in same_city):
                    return rank_locations(same_city, names)
        except ProviderError as exc:
            log.info("open-meteo geocoding failed (%s); using the gazetteer entry", exc.code)
        return [location_from_gazetteer(place)]

    async def resolve(self, city: str) -> list[Location]:
        """Matching locations, best first. Cities in the shared gazetteer (data/gazetteer.yaml) are resolved
        in their own country first ("Bangalore" is Bengaluru, India, not Bangalore Town, Pakistan); other
        names are ranked by exact name match, then population. ``"City, Country"`` narrows the search."""
        text = re.sub(r"\s+", " ", (city or "").strip())
        if not text:
            return []
        name, _, hint = (part.strip() for part in text.partition(","))
        if len(name) < 2:
            return []
        hint_cc = _country_code(hint) if hint else None
        place = gazetteer_city(name)
        if place is not None and (not hint or hint_cc == place.country_code):
            return await self._resolve_known(name, place)

        names = [name]
        results = await self._search(name, hint_cc)
        if not results:
            alias = CITY_ALIASES.get(name.lower())
            if alias:
                names.append(alias)
                results = await self._search(alias, hint_cc)
        if hint and not hint_cc and results:  # a region rather than a country ("London, Ontario")
            key = _fold(hint)
            narrowed = [r for r in results if key in _fold(r.admin1 or "")]
            results = narrowed or results
        return rank_locations(results, names)


def _local_minutes(value: Any, day: date) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.date() != day:
        return None
    return dt.hour * 60 + dt.minute


def _first(daily: dict[str, Any], key: str) -> Any:
    values = daily.get(key)
    return values[0] if isinstance(values, list) and values else None


class OpenMeteoWeather:
    """WeatherProvider over the Open-Meteo forecast API. Never raises: failures -> status "unavailable"."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def forecast(self, location: Location, day: date) -> Weather:
        params: dict[str, str | float] = {
            "latitude": round(location.lat, 4),
            "longitude": round(location.lng, 4),
            "daily": DAILY_FIELDS,
            "timezone": "auto",
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        }
        try:
            resp = await self._client.get(FORECAST_URL, params=params, timeout=TIMEOUT)
            if resp.status_code != 200:
                log.info("open-meteo forecast HTTP %s", resp.status_code)
                return _unavailable()
            return _parse_forecast(resp.json(), day)
        except Exception:  # network, JSON or shape problems: the plan proceeds without weather
            return _unavailable()


def _parse_forecast(data: Any, day: date) -> Weather:
    daily = data.get("daily") if isinstance(data, dict) else None
    if not isinstance(daily, dict):
        return _unavailable()
    if _first(daily, "time") != day.isoformat():
        return _unavailable()
    code = _first(daily, "weather_code")
    tmax = _first(daily, "temperature_2m_max")
    tmin = _first(daily, "temperature_2m_min")
    precip = _first(daily, "precipitation_probability_max")
    if code is None and tmax is None and precip is None:
        return _unavailable()
    try:
        weather_code = int(code) if code is not None else None
        precip_pct = round(float(precip)) if precip is not None else None
        temp_max = float(tmax) if tmax is not None else None
        temp_min = float(tmin) if tmin is not None else None
    except (TypeError, ValueError):
        return _unavailable()
    return Weather(
        status="ok",
        summary=weather_summary(weather_code, temp_max, precip_pct),
        precip_prob_max=precip_pct,
        temp_max_c=temp_max,
        temp_min_c=temp_min,
        weather_code=weather_code,
        sunrise_min=_local_minutes(_first(daily, "sunrise"), day),
        sunset_min=_local_minutes(_first(daily, "sunset"), day),
    )
