"""Curated snapshot provider over ``backend/data/snapshot/*.json`` (OpenStreetMap-derived, ODbL).

Used when the live places provider is down or over quota, and as the keyless default. OSM facts (name,
position, diet tags, wheelchair tag, opening_hours) are mapped honestly: unknown stays unknown. Costs are
category defaults labelled ``category_estimate`` (OSM rarely has prices). Each candidate links to its OSM
object, carries the snapshot's ``last_checked`` date and "© OpenStreetMap contributors" attribution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.domain.categories import DATA_DIR, category
from app.domain.schemas import Candidate, CostRange, Evidence, Location, PlaceQuery, Source, WorldContext
from app.providers.normalize import (
    busy_windows_for,
    category_default_cost,
    dedupe,
    location_currency,
    maps_search_url,
    parse_osm_opening_hours,
    search_local,
    within_city,
)

SNAPSHOT_DIR = DATA_DIR / "snapshot"
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
_FOOD_GROUPS = {"food", "drinks"}


@dataclass(frozen=True)
class Snapshot:
    city: dict[str, Any]
    last_checked: date
    places: list[dict[str, Any]]
    neighbourhoods: dict[str, tuple[float, float]]


@lru_cache(maxsize=4)
def load_snapshots(directory: str | None = None) -> tuple[Snapshot, ...]:
    folder = Path(directory) if directory else SNAPSHOT_DIR
    out: list[Snapshot] = []
    for file in sorted(folder.glob("*.json")):
        raw = json.loads(file.read_text(encoding="utf-8"))
        hoods = {name.lower(): (float(lat), float(lng)) for name, (lat, lng) in raw.get("neighbourhoods", {}).items()}
        out.append(
            Snapshot(
                city=raw["city"],
                last_checked=date.fromisoformat(raw["last_checked"]),
                places=list(raw["places"]),
                neighbourhoods=hoods,
            )
        )
    return tuple(out)


def evidence_from_osm(value: str | None) -> Evidence:
    """OSM ``diet:*`` / ``wheelchair`` value -> Evidence. yes/only -> CONFIRMED, no -> NO, else UNKNOWN
    (``limited`` wheelchair access is not a yes)."""
    if value is None:
        return Evidence.UNKNOWN
    v = value.strip().lower()
    if v in {"yes", "only", "designated"}:
        return Evidence.CONFIRMED
    if v == "no":
        return Evidence.NO
    return Evidence.UNKNOWN


def diet_from_osm(tags: dict[str, str]) -> dict[str, Evidence]:
    """Diet evidence from OSM tags. A place with vegan options (``diet:vegan`` yes/only) necessarily has
    vegetarian options, so vegetarian is CONFIRMED in that case even without a ``diet:vegetarian`` tag."""
    diet = {
        "vegetarian": evidence_from_osm(tags.get("diet:vegetarian")),
        "vegan": evidence_from_osm(tags.get("diet:vegan")),
        "jain": evidence_from_osm(tags.get("diet:jain")),
    }
    if diet["vegetarian"] == Evidence.UNKNOWN and diet["vegan"] == Evidence.CONFIRMED:
        diet["vegetarian"] = Evidence.CONFIRMED
    return diet


def wheelchair_from_osm(value: str | None) -> Evidence:
    return evidence_from_osm(value)


def _osm_url(record: dict[str, Any]) -> str:
    return f"https://www.openstreetmap.org/{record['osm_type']}/{record['osm_id']}"


def snapshot_candidate(record: dict[str, Any], snap: Snapshot, ctx: WorldContext) -> Candidate:
    """Build a Candidate for ``ctx`` (hours for ``ctx.date``'s weekday, costs in the city currency)."""
    tags: dict[str, str] = record.get("osm_tags", {})
    key = record["category"]
    info = category(key)
    currency = snap.city.get("currency") or location_currency(ctx.location)
    if tags.get("fee") == "no" and info.group not in _FOOD_GROUPS:
        cost = CostRange(min=0, max=0, currency=currency, basis="free")
    else:
        cost = category_default_cost(key, currency)
    extra_tags: list[str] = []
    for cuisine in (tags.get("cuisine") or "").split(";"):
        if cuisine.strip():
            extra_tags.append(cuisine.strip().replace("_", " "))
    if tags.get("diet:vegetarian") == "only":
        extra_tags.append("vegetarian only")
    if tags.get("diet:vegan") == "only":
        extra_tags.append("vegan only")
    if tags.get("fee") == "no":
        extra_tags.append("free")
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p) or None
    popularity = record.get("popularity", "medium")
    return Candidate(
        id=record["id"],
        kind="place",
        name=record["name"],
        category=key,
        city_id=ctx.location.id,
        lat=record["lat"],
        lng=record["lng"],
        address=street,
        cost=cost,
        hours=parse_osm_opening_hours(tags.get("opening_hours"), ctx.date.weekday()),
        diet=diet_from_osm(tags) if info.group in _FOOD_GROUPS else {},
        wheelchair=wheelchair_from_osm(tags.get("wheelchair")),
        indoor=info.indoor,
        seated=info.seated,
        effort=info.effort,
        serves_alcohol=True if info.alcohol else None,
        live_music=True if key == "live_music_venue" else None,
        popularity=popularity,
        busy_windows=busy_windows_for(key, popularity),
        tags=dedupe([*info.tags, *extra_tags]),
        duration_range=info.duration,
        maps_url=maps_search_url(record["name"], record["lat"], record["lng"]),
        source=Source(
            provider="curated_snapshot",
            url=_osm_url(record),
            last_checked=snap.last_checked,
            attribution=OSM_ATTRIBUTION,
        ),
    )


class SnapshotPlaces:
    """PlacesProvider over the curated OSM snapshots. Covers a location within 40 km of a snapshot city."""

    name = "curated_snapshot"

    def __init__(self, directory: str | Path | None = None) -> None:
        self._snapshots = load_snapshots(str(directory) if directory else None)

    def _covering(self, location: Location) -> list[Snapshot]:
        return [s for s in self._snapshots if within_city(float(s.city["lat"]), float(s.city["lng"]), location)]

    def covers(self, location: Location) -> bool:
        return bool(self._covering(location))

    def all_candidates(self, ctx: WorldContext) -> list[Candidate]:
        return [snapshot_candidate(r, s, ctx) for s in self._covering(ctx.location) for r in s.places]

    async def search(self, query: PlaceQuery, ctx: WorldContext) -> list[Candidate]:
        snaps = self._covering(ctx.location)
        pool = [snapshot_candidate(r, s, ctx) for s in snaps for r in s.places]
        hoods: dict[str, tuple[float, float]] = {}
        for s in snaps:
            hoods.update(s.neighbourhoods)
        return search_local(pool, query, ctx, hoods)

    async def get(self, candidate_id: str, ctx: WorldContext) -> Candidate | None:
        for snap in self._covering(ctx.location):
            for record in snap.places:
                if record["id"] == candidate_id:
                    return snapshot_candidate(record, snap, ctx)
        return None
