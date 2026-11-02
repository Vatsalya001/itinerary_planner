"""Open-Meteo geocoding and daily forecast (no key; free tier is non-commercial, see SOURCES.md)."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

import httpx

from app.domain.categories import currency_for_country
from app.domain.schemas import Location, Weather
from app.providers.base import ProviderError
from app.providers.normalize import weather_summary

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 8.0
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

    async def _search(self, name: str) -> list[Location]:
        params: dict[str, str | int] = {"name": name, "count": 5, "language": "en", "format": "json"}
        try:
            resp = await self._client.get(GEOCODE_URL, params=params, timeout=TIMEOUT_S)
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
        out: list[Location] = []
        for result in data.get("results") or []:
            loc = _location_from(result) if isinstance(result, dict) else None
            if loc is not None:
                out.append(loc)
        return out

    async def resolve(self, city: str) -> list[Location]:
        text = re.sub(r"\s+", " ", (city or "").strip())
        if not text:
            return []
        name, _, country_hint = (part.strip() for part in text.partition(","))
        if len(name) < 2:
            return []
        results = await self._search(name)
        if not results:
            alias = CITY_ALIASES.get(name.lower())
            if alias:
                results = await self._search(alias)
        if country_hint and results:
            hint = country_hint.lower()
            narrowed = [r for r in results if r.country_code.lower() == hint or hint in (r.admin1 or "").lower()]
            results = narrowed or results
        return results


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
            resp = await self._client.get(FORECAST_URL, params=params, timeout=TIMEOUT_S)
            if resp.status_code != 200:
                log.info("open-meteo forecast HTTP %s", resp.status_code)
                return _unavailable()
            daily = resp.json().get("daily") or {}
        except (httpx.HTTPError, ValueError, AttributeError):
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
