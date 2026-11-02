"""Deterministic travel estimates between candidates (haversine x detour / per-city mode speeds).

OWNER: domain agent (Phase 1). Reads data/cities.yaml (speeds, pickup minutes, fare tables per city
with source and date; a "default" entry for unknown cities). Contract only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.domain.categories import currency_for_country
from app.domain.schemas import Candidate, CostRange, Leg, TravelMode, WorldContext

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CITIES_PATH = DATA_DIR / "cities.yaml"

EARTH_RADIUS_KM = 6371.0088
DETOUR_FACTOR = 1.4
WALK_MAX_KM = 1.2  # route distance (after the detour factor) at or below which a leg is walked
WALK_SPEED_KMH = 4.5
MINUTES_MAX_FACTOR = 1.3
FARE_MAX_FACTOR = 1.25


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km. NaN in, NaN out."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def coords_valid(lat: float | None, lng: float | None) -> bool:
    """True for finite, in-range coordinates. (0, 0) is treated as a missing-value placeholder."""
    if lat is None or lng is None:
        return False
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return False
    return not (lat == 0.0 and lng == 0.0)


class TransferUnknownError(ValueError):
    """Raised when a leg cannot be estimated (missing or invalid coordinates)."""


@dataclass(frozen=True)
class Fare:
    currency: str
    base: float
    included_km: float
    per_km: float
    source: str
    as_of: str

    def amount(self, km: float) -> float:
        return self.base + max(0.0, km - self.included_km) * self.per_km


@dataclass(frozen=True)
class CityTravel:
    key: str
    speeds_kmh: dict[str, float]
    pickup_min: dict[str, float]
    motor_mode: TravelMode
    fares: dict[str, Fare]  # mode -> fare, only in the location's currency
    currency: str


@lru_cache(maxsize=8)
def _load(path: str) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return raw


def _fare(entry: dict[str, Any], currency: str) -> Fare:
    return Fare(
        currency=currency,
        base=float(entry["base"]),
        included_km=float(entry.get("included_km", 0.0)),
        per_km=float(entry["per_km"]),
        source=str(entry.get("source", "estimate (unverified)")),
        as_of=str(entry.get("as_of", "")),
    )


class HeuristicTravel:
    """Satisfies app.providers.base.TravelEstimator.

    Rules: distance = haversine x 1.4. Walk if <= 1.2 km (4.5 km/h). Otherwise auto (cab when the city
    has no autos) at the city's speed plus pickup minutes. minutes_max = ceil(minutes x 1.3).
    Fare from the city fare table (base + per-km), as a CostRange with basis "category_estimate".
    """

    def __init__(self, cities_path: str | None = None) -> None:
        self._raw = _load(str(cities_path or CITIES_PATH))
        self._alias: dict[str, list[str]] = {}
        for key, entry in self._raw.items():
            if key.startswith("default"):
                continue
            for name in [key, *(entry.get("aliases") or [])]:
                self._alias.setdefault(str(name).lower(), []).append(key)
        self._cache: dict[tuple[str, str], CityTravel] = {}

    # -- configuration -----------------------------------------------------------------------------

    def city_key(self, ctx: WorldContext) -> str:
        """Named city only when both the name (or alias) and the country match; else default_IN / default."""
        name = ctx.location.name.strip().lower()
        country = ctx.location.country_code.upper()
        for key in self._alias.get(name, []):
            if str(self._raw[key].get("country", "")).upper() == country:
                return key
        if country == "IN" and "default_IN" in self._raw:
            return "default_IN"
        return "default"

    @staticmethod
    def location_currency(ctx: WorldContext) -> str:
        """The currency legs are priced in: the same rule place providers use (location, country, else USD)."""
        return ctx.location.currency or currency_for_country(ctx.location.country_code) or "USD"

    def config(self, ctx: WorldContext) -> CityTravel:
        key = self.city_key(ctx)
        currency = self.location_currency(ctx)
        cache_key = (key, currency)
        if cache_key in self._cache:
            return self._cache[cache_key]
        entry = self._raw[key]
        fares: dict[str, Fare] = {}
        if "fares" in entry:
            for mode, f in entry["fares"].items():
                if str(f.get("currency") or currency) == currency:  # never price in another currency
                    fares[mode] = _fare(f, currency)
        else:
            by_cur: dict[str, Any] = entry.get("fares_by_currency", {})
            if currency in by_cur:
                fares[str(entry.get("motor_mode", "cab"))] = _fare(by_cur[currency], currency)
        cfg = CityTravel(
            key=key,
            speeds_kmh={k: float(v) for k, v in entry["speeds_kmh"].items()},
            pickup_min={k: float(v) for k, v in (entry.get("pickup_min") or {}).items()},
            motor_mode=entry.get("motor_mode", "auto"),
            fares=fares,
            currency=currency,
        )
        self._cache[cache_key] = cfg
        return cfg

    # -- estimation --------------------------------------------------------------------------------

    def leg(self, a: Candidate, b: Candidate, ctx: WorldContext) -> Leg:
        """Raises TransferUnknownError for missing coordinates, or for a motorised leg in a currency with no fare
        table (fares are never priced in a different currency from the location's)."""
        if not (coords_valid(a.lat, a.lng) and coords_valid(b.lat, b.lng)):
            raise TransferUnknownError(f"missing coordinates for {a.id} or {b.id}")
        cfg = self.config(ctx)
        km = round(haversine_km(a.lat, a.lng, b.lat, b.lng) * DETOUR_FACTOR, 2)
        if km <= WALK_MAX_KM:
            speed = cfg.speeds_kmh.get("walk", WALK_SPEED_KMH)
            minutes = math.ceil(km / speed * 60)
            return Leg(
                from_id=a.id, to_id=b.id, mode="walk", distance_km=km, minutes=minutes,
                minutes_max=math.ceil(minutes * MINUTES_MAX_FACTOR),
                fare=CostRange(min=0, max=0, currency=cfg.currency, basis="free"),
            )
        mode: TravelMode = cfg.motor_mode
        if mode not in cfg.fares:
            mode = "cab"
        if mode not in cfg.fares or mode not in cfg.speeds_kmh:
            raise TransferUnknownError(f"no {cfg.currency} fare table for motorised travel in {ctx.location.name}")
        speed = cfg.speeds_kmh[mode]
        minutes = math.ceil(cfg.pickup_min.get(mode, 0.0) + km / speed * 60)
        fare = cfg.fares[mode]
        amount = fare.amount(km)
        return Leg(
            from_id=a.id, to_id=b.id, mode=mode, distance_km=km, minutes=minutes,
            minutes_max=math.ceil(minutes * MINUTES_MAX_FACTOR),
            fare=CostRange(
                min=round(amount), max=round(amount * FARE_MAX_FACTOR), currency=fare.currency,
                basis="category_estimate",
            ),
        )
