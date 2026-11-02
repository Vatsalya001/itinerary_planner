from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date

import pytest

from app.domain.categories import is_category
from app.domain.schemas import Evidence, Location, PlaceQuery, WorldContext
from app.providers.base import PlacesProvider
from app.providers.normalize import haversine_m
from app.providers.snapshot import (
    SNAPSHOT_DIR,
    SnapshotPlaces,
    diet_from_osm,
    evidence_from_osm,
    load_snapshots,
    wheelchair_from_osm,
)

CtxFactory = Callable[..., WorldContext]
RAW = json.loads((SNAPSHOT_DIR / "bangalore.json").read_text(encoding="utf-8"))


def test_protocol_and_name() -> None:
    sp = SnapshotPlaces()
    assert isinstance(sp, PlacesProvider) and sp.name == "curated_snapshot"


def test_snapshot_metadata_records_source_and_licence() -> None:
    src = RAW["source"]
    assert "Overpass" in src["provider"] and src["endpoint"].startswith("https://overpass-api.de/")
    assert "ODbL" in src["licence"] and src["attribution"] == "© OpenStreetMap contributors"
    assert src["retrieved"] == "2026-11-02" and RAW["last_checked"] == "2026-11-02"
    assert 30 <= len(RAW["places"]) <= 45


def test_records_are_osm_backed() -> None:
    ids = [p["id"] for p in RAW["places"]]
    assert len(ids) == len(set(ids))
    for p in RAW["places"]:
        assert re.fullmatch(r"snap:osm-(node|way|relation)-\d+", p["id"])
        assert p["id"] == f"snap:osm-{p['osm_type']}-{p['osm_id']}"
        assert is_category(p["category"])
        assert p["osm_tags"].get("name") == p["name"]
        assert "cost" not in p  # never invented: providers use labelled category defaults
        assert haversine_m(p["lat"], p["lng"], RAW["city"]["lat"], RAW["city"]["lng"]) <= 15_000


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yes", Evidence.CONFIRMED), ("only", Evidence.CONFIRMED), ("no", Evidence.NO), ("limited", Evidence.UNKNOWN),
     (None, Evidence.UNKNOWN), ("maybe", Evidence.UNKNOWN), (" YES ", Evidence.CONFIRMED)],
)
def test_evidence_from_osm(value: str | None, expected: Evidence) -> None:
    assert evidence_from_osm(value) == expected


def test_diet_and_wheelchair_mapping() -> None:
    assert diet_from_osm({}) == {"vegetarian": Evidence.UNKNOWN, "vegan": Evidence.UNKNOWN, "jain": Evidence.UNKNOWN}
    assert diet_from_osm({"diet:vegetarian": "only"})["vegetarian"] == Evidence.CONFIRMED
    assert diet_from_osm({"diet:vegetarian": "no"})["vegetarian"] == Evidence.NO
    vegan = diet_from_osm({"diet:vegan": "yes"})
    assert vegan["vegan"] == Evidence.CONFIRMED and vegan["vegetarian"] == Evidence.CONFIRMED
    mixed = diet_from_osm({"diet:vegetarian": "only", "diet:vegan": "no"})
    assert (mixed["vegetarian"], mixed["vegan"]) == (Evidence.CONFIRMED, Evidence.NO)
    assert wheelchair_from_osm("yes") == Evidence.CONFIRMED
    assert wheelchair_from_osm("no") == Evidence.NO
    assert wheelchair_from_osm("limited") == Evidence.UNKNOWN


def test_covers(locations: dict[str, Location]) -> None:
    sp = SnapshotPlaces()
    assert sp.covers(locations["blr"])
    assert not sp.covers(locations["jai"]) and not sp.covers(locations["bom"]) and not sp.covers(locations["lon"])


async def test_uncovered_city_returns_nothing(ctx_factory: CtxFactory, locations: dict[str, Location]) -> None:
    assert await SnapshotPlaces().search(PlaceQuery(intent="cafe"), ctx_factory(locations["jai"])) == []


def test_candidates_are_honest(blr_ctx: WorldContext) -> None:
    snap = load_snapshots()[0]
    raw_by_id = {p["id"]: p for p in snap.places}
    cands = SnapshotPlaces().all_candidates(blr_ctx)
    assert len(cands) == len(snap.places)
    for c in cands:
        tags = raw_by_id[c.id]["osm_tags"]
        assert c.city_id == blr_ctx.location.id
        assert c.source.provider == "curated_snapshot"
        assert c.source.attribution == "© OpenStreetMap contributors"
        assert c.source.last_checked == date(2026, 11, 2)
        assert c.source.url == f"https://www.openstreetmap.org/{raw_by_id[c.id]['osm_type']}/{raw_by_id[c.id]['osm_id']}"
        assert c.cost.basis in {"category_estimate", "free"}
        if "opening_hours" not in tags:
            assert c.hours.status == "unknown"
        if "wheelchair" not in tags:
            assert c.wheelchair == Evidence.UNKNOWN
        if not any(k.startswith("diet:") for k in tags):
            assert all(v == Evidence.UNKNOWN for v in c.diet.values())
        if c.diet.get("vegetarian") == Evidence.CONFIRMED:
            assert tags.get("diet:vegetarian") in {"yes", "only"} or tags.get("diet:vegan") in {"yes", "only"}


def test_parsed_hours_for_saturday(blr_ctx: WorldContext) -> None:
    by_name = {c.name: c for c in SnapshotPlaces().all_candidates(blr_ctx)}
    assert by_name["Hard Rock Cafe"].hours.periods == [(0, 60), (720, 1500)]  # Mo-Su 12:00-01:00
    assert by_name["Baisakhi Vegetarian Restaurant"].hours.periods == [(750, 960), (1140, 1380)]
    assert by_name["Sandesha Museum of Communication"].hours.periods == [(600, 960)]  # Mo-Sa
    assert by_name["Select Bookshop"].hours.periods == [(690, 1140)]


async def test_outage_path_has_a_vegetarian_plan_near_the_centre(blr_ctx: WorldContext) -> None:
    sp = SnapshotPlaces()
    food = await sp.search(PlaceQuery(intent="vegetarian restaurant", radius_m=3000), blr_ctx)
    assert food and food[0].diet["vegetarian"] == Evidence.CONFIRMED
    parks = await sp.search(PlaceQuery(intent="park walk", radius_m=3000), blr_ctx)
    assert any(c.name == "Cubbon Park" for c in parks)
    assert all(c.source.provider == "curated_snapshot" for c in food + parks)


async def test_get(blr_ctx: WorldContext) -> None:
    sp = SnapshotPlaces()
    c = await sp.get("snap:osm-way-22895320", blr_ctx)
    assert c is not None and c.name == "Cubbon Park" and c.category == "park"
    assert await sp.get("snap:osm-node-1", blr_ctx) is None


async def test_query_neighbourhood_centres_snapshot_search(blr_ctx: WorldContext) -> None:
    sp = SnapshotPlaces()
    plain = await sp.search(PlaceQuery(intent="vegan cafe", radius_m=1500), blr_ctx)
    assert all("Copper" not in c.name for c in plain)  # Indiranagar is ~5 km from the centre
    indira = await sp.search(PlaceQuery(intent="vegan cafe", neighbourhood="Indiranagar", radius_m=1500), blr_ctx)
    assert indira and indira[0].diet["vegan"] == Evidence.CONFIRMED  # ranked first; diet is not a filter
    assert any("Copper + Cloves" in c.name for c in indira)
    # a neighbourhood only the shared gazetteer knows still centres the search
    jaya = await sp.search(PlaceQuery(intent="", neighbourhood="Jayanagar", radius_m=3000), blr_ctx)
    assert jaya and all(haversine_m(c.lat, c.lng, 12.9250, 77.5938) <= 3000 for c in jaya)
