"""Frozen-world fixture providers (offline tests and every model eval).

Data lives in ``backend/data/fixtures``: ``world.json`` (locations, neighbourhood centroids, candidates),
``events.json`` (schema.org Event JSON-LD per scenario) and ``weather.json`` (per scenario and city).
Place names and coordinates may be real; every other attribute is synthetic and labelled
"Synthetic test attributes". Fixture opening hours describe Saturdays only (the planner always targets a
Saturday); on any other weekday hours are reported as unknown rather than guessed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.domain.categories import DATA_DIR, category
from app.domain.schemas import (
    Candidate,
    CostRange,
    Hours,
    Location,
    PlaceQuery,
    Source,
    Weather,
    WorldContext,
)
from app.providers.events_web import event_to_candidate, filter_and_rank_events
from app.providers.normalize import (
    busy_windows_for,
    category_default_cost,
    dedupe,
    is_https_url,
    maps_search_url,
    search_local,
    weather_summary,
    within_city,
)

FIXTURES_DIR = DATA_DIR / "fixtures"
FIXTURE_ATTRIBUTION = "Synthetic test attributes"
FIXTURE_EVENT_ATTRIBUTION = "Synthetic test event"
SATURDAY = 5
DEFAULT_EVENT_SCENARIOS: tuple[str, ...] = ("valid_same_day", "wrong_date", "no_source_url")
WEATHER_SCENARIOS = ("clear", "rain_85pct", "unavailable")


@dataclass
class FixtureWorld:
    locations: dict[str, Location]
    aliases: dict[str, str]  # normalized alias -> location id
    candidates: dict[str, list[Candidate]]  # city id -> candidates (Saturday hours)
    neighbourhoods: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)

    def all_candidates(self) -> list[Candidate]:
        return [c for cands in self.candidates.values() for c in cands]


def _norm_city(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _candidate_from_raw(raw: dict[str, Any], loc: Location) -> Candidate:
    data = {k: v for k, v in raw.items() if not k.startswith("_") and k != "city"}
    info = category(data["category"])
    currency = loc.currency or "INR"
    cost_raw = data.pop("cost", None)
    if cost_raw is None:
        cost = category_default_cost(data["category"], currency)
    else:
        lo, hi, basis = cost_raw
        cost = CostRange(min=lo, max=hi, currency=currency, basis=basis)
    hours_raw = data.pop("hours", None)
    hours = Hours(status="unknown") if hours_raw is None else Hours(status="known", periods=[tuple(p) for p in hours_raw])
    popularity = data.get("popularity", "medium")
    data.setdefault("indoor", info.indoor)
    data.setdefault("seated", info.seated)
    data.setdefault("effort", info.effort)
    data.setdefault("serves_alcohol", info.alcohol)
    data.setdefault("busy_windows", busy_windows_for(data["category"], popularity))
    data["tags"] = dedupe([*info.tags, *data.get("tags", [])])
    data.setdefault("duration_range", info.duration)
    data.setdefault("maps_url", maps_search_url(data["name"], data["lat"], data["lng"]))
    return Candidate(
        **data,
        city_id=loc.id,
        cost=cost,
        hours=hours,
        source=Source(provider="fixture", attribution=FIXTURE_ATTRIBUTION),
    )


@lru_cache(maxsize=4)
def load_world(path: str | None = None) -> FixtureWorld:
    file = Path(path) if path else FIXTURES_DIR / "world.json"
    raw = json.loads(file.read_text(encoding="utf-8"))
    locations: dict[str, Location] = {}
    aliases: dict[str, str] = {}
    for entry in raw["locations"]:
        data = {k: v for k, v in entry.items() if k != "aliases" and not k.startswith("_")}
        loc = Location(**data)
        locations[loc.id] = loc
        for alias in [loc.name, loc.id.split(":", 1)[-1], *entry.get("aliases", [])]:
            aliases[_norm_city(alias)] = loc.id
    candidates: dict[str, list[Candidate]] = {loc_id: [] for loc_id in locations}
    for entry in raw["candidates"]:
        loc = locations[entry["city"]]
        candidates[loc.id].append(_candidate_from_raw(entry, loc))
    hoods: dict[str, dict[str, tuple[float, float]]] = {}
    for city_id, table in raw.get("neighbourhoods", {}).items():
        hoods[city_id] = {_norm_city(name): (float(lat), float(lng)) for name, (lat, lng) in table.items()}
    return FixtureWorld(locations=locations, aliases=aliases, candidates=candidates, neighbourhoods=hoods)


def _for_day(c: Candidate, day: date) -> Candidate:
    """Fixture hours are Saturday hours: on other weekdays report them as unknown."""
    if day.weekday() == SATURDAY or c.kind == "event":
        return c
    return c.model_copy(update={"hours": Hours(status="unknown")})


class FixturePlaces:
    """PlacesProvider over the frozen world. Candidates carry ``city_id = ctx.location.id``."""

    name = "fixture"

    def __init__(self, path: str | Path | None = None) -> None:
        self._world = load_world(str(path) if path else None)

    def _cities_for(self, location: Location) -> list[str]:
        return [
            loc_id
            for loc_id, loc in self._world.locations.items()
            if self._world.candidates.get(loc_id) and within_city(loc.lat, loc.lng, location)
        ]

    def covers(self, location: Location) -> bool:
        return bool(self._cities_for(location))

    def _pool(self, ctx: WorldContext) -> tuple[list[Candidate], dict[str, tuple[float, float]]]:
        pool: list[Candidate] = []
        hoods: dict[str, tuple[float, float]] = {}
        for city_id in self._cities_for(ctx.location):
            pool.extend(_for_day(c, ctx.date) for c in self._world.candidates[city_id])
            hoods.update(self._world.neighbourhoods.get(city_id, {}))
        return pool, hoods

    async def search(self, query: PlaceQuery, ctx: WorldContext) -> list[Candidate]:
        pool, hoods = self._pool(ctx)
        return search_local(pool, query, ctx, hoods)

    async def get(self, candidate_id: str, ctx: WorldContext) -> Candidate | None:
        pool, _ = self._pool(ctx)
        for c in pool:
            if c.id == candidate_id:
                return c.model_copy(update={"city_id": ctx.location.id})
        return None

    def all_candidates(self, ctx: WorldContext) -> list[Candidate]:
        """Every candidate in the context's city (for the deterministic planner and eval assertions)."""
        pool, _ = self._pool(ctx)
        return [c.model_copy(update={"city_id": ctx.location.id}) for c in pool]


class FixtureGeocoder:
    """Geocoder over world.json locations: case-insensitive names and aliases; unknown -> []."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._world = load_world(str(path) if path else None)

    async def resolve(self, city: str) -> list[Location]:
        if not city or not city.strip():
            return []
        keys = [_norm_city(city)]
        if "," in city:
            keys.append(_norm_city(city.split(",", 1)[0]))
        for key in keys:
            loc_id = self._world.aliases.get(key)
            if loc_id:
                return [self._world.locations[loc_id]]
        return []


@lru_cache(maxsize=4)
def _load_json(path: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return data


class FixtureWeather:
    """WeatherProvider for scenarios "clear", "rain_85pct" and "unavailable" (frozen: any day returns the
    scenario's values for the city). Unknown cities return status "unavailable"."""

    def __init__(self, scenario: str = "clear", path: str | Path | None = None) -> None:
        if scenario not in WEATHER_SCENARIOS:
            raise ValueError(f"unknown weather scenario {scenario!r}; expected one of {WEATHER_SCENARIOS}")
        self.scenario = scenario
        self._data = _load_json(str(path or FIXTURES_DIR / "weather.json"))
        self._world = load_world()

    async def forecast(self, location: Location, day: date) -> Weather:
        table = self._data["scenarios"].get(self.scenario)
        if not table:
            return Weather(status="unavailable", summary="Weather not available")
        entry = table.get(location.id)
        if entry is None:  # a non-fixture location id: use a fixture city within 40 km, if any
            for loc_id, loc in self._world.locations.items():
                if loc_id in table and within_city(loc.lat, loc.lng, location):
                    entry = table[loc_id]
                    break
        if entry is None:
            return Weather(status="unavailable", summary="Weather not available")
        return Weather(
            status="ok",
            summary=weather_summary(entry["weather_code"], entry["temp_max_c"], entry["precip_prob_max"]),
            precip_prob_max=entry["precip_prob_max"],
            temp_max_c=entry["temp_max_c"],
            temp_min_c=entry["temp_min_c"],
            weather_code=entry["weather_code"],
            sunrise_min=entry["sunrise_min"],
            sunset_min=entry["sunset_min"],
        )


class FixtureEvents:
    """EventsProvider over events.json scenarios (schema.org JSON-LD run through the web extractor).

    ``search`` applies the provider rules: only events on ``ctx.date`` with an https source URL, in the
    context's city. ``all_candidates`` skips those rules so validator tests can see the bad events."""

    name = "fixture_event"

    def __init__(self, scenarios: list[str] | tuple[str, ...] | None = None, path: str | Path | None = None) -> None:
        data = _load_json(str(path or FIXTURES_DIR / "events.json"))
        self.scenarios = list(scenarios) if scenarios is not None else list(DEFAULT_EVENT_SCENARIOS)
        missing = [s for s in self.scenarios if s not in data["scenarios"]]
        if missing:
            raise KeyError(f"unknown event fixture scenario(s): {missing}")
        self._records = [data["scenarios"][s] for s in self.scenarios]
        self._world = load_world()

    def _in_city(self, record: dict[str, Any], ctx: WorldContext) -> bool:
        loc = self._world.locations.get(record.get("city", ""))
        return loc is not None and within_city(loc.lat, loc.lng, ctx.location)

    def _convert(self, ctx: WorldContext, strict: bool) -> list[Candidate]:
        out: list[Candidate] = []
        for record in self._records:
            if not self._in_city(record, ctx):
                continue
            page_url = record.get("page_url")
            cand = event_to_candidate(
                record["jsonld"],
                page_url if (not strict or is_https_url(page_url)) else None,
                ctx,
                provider="fixture_event",
                id_prefix="evfx:",
                attribution=FIXTURE_EVENT_ATTRIBUTION,
                require_same_day=strict,
                require_https=strict,
            )
            if cand is not None:
                out.append(cand)
        return out

    async def search(self, interests: list[str], ctx: WorldContext, max_price: int | None = None) -> list[Candidate]:
        return filter_and_rank_events(self._convert(ctx, strict=True), interests, max_price)

    def all_candidates(self, ctx: WorldContext) -> list[Candidate]:
        return self._convert(ctx, strict=False)
