"""Astronomical sunrise and sunset (pure function of date, coordinates and timezone).

NOAA's simplified solar-position equations, accurate to a few minutes outside the polar regions.

* ``sun_times(day, lat, lng, tz)`` -> (sunrise_min, sunset_min) in local minutes since midnight, or None for
  polar day/night or bad input. app.pipeline uses it to fill the sunset when the forecast is unavailable.
* ``effective_sunset(ctx)`` is the validator/planner backstop: the forecast's sunset when present, else the
  computed sunset minus ``SAFETY_MARGIN_MIN`` (conservative, so formula error never allows a walk after dark).

NOTE (domain agent): this file was accidentally overwritten during the D-04 work and rebuilt with the same
public API (``sun_times``) that app.pipeline and tests/unit/test_sun.py use.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.domain.schemas import WorldContext

SAFETY_MARGIN_MIN = 15
_ZENITH_DEG = 90.833  # sun's upper limb at the horizon, with standard refraction


def _utc_offset_min(day: date, tz: str) -> float | None:
    try:
        offset = ZoneInfo(tz).utcoffset(datetime(day.year, day.month, day.day, 12))
    except Exception:  # unknown timezone name
        return None
    return None if offset is None else offset.total_seconds() / 60


def sun_times(day: date, lat: float, lng: float, tz: str) -> tuple[int, int] | None:
    """(sunrise, sunset) in local minutes since midnight of ``day``; None in polar day/night or on bad input."""
    if not (math.isfinite(lat) and math.isfinite(lng)) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    offset = _utc_offset_min(day, tz)
    if offset is None:
        return None
    gamma = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
            + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    phi = math.radians(lat)
    denom = math.cos(phi) * math.cos(decl)
    if abs(denom) < 1e-12:
        return None
    cos_ha = math.cos(math.radians(_ZENITH_DEG)) / denom - math.tan(phi) * math.tan(decl)
    if not -1.0 <= cos_ha <= 1.0:
        return None
    ha = math.degrees(math.acos(cos_ha))
    sunrise_utc = 720 - 4 * (lng + ha) - eqtime
    sunset_utc = 720 - 4 * (lng - ha) - eqtime
    return round(sunrise_utc + offset), round(sunset_utc + offset)


def computed_sunset_min(lat: float, lng: float, day: date, tz: str) -> int | None:
    times = sun_times(day, lat, lng, tz)
    return None if times is None else times[1]


def effective_sunset(ctx: WorldContext) -> int | None:
    """The forecast's sunset, else a conservative computed one (computed sunset minus a safety margin)."""
    if ctx.weather.sunset_min is not None:
        return ctx.weather.sunset_min
    computed = computed_sunset_min(ctx.location.lat, ctx.location.lng, ctx.date, ctx.location.timezone)
    return None if computed is None else computed - SAFETY_MARGIN_MIN
