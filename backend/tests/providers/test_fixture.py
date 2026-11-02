from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from app.domain.schemas import Location, PlaceQuery, WorldContext
from app.providers.base import EventsProvider, Geocoder, PlacesProvider, WeatherProvider
from app.providers.fixture import FixtureEvents, FixtureGeocoder, FixturePlaces, FixtureWeather
from app.providers.normalize import weather_summary

CtxFactory = Callable[..., WorldContext]

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_fixture_providers_satisfy_protocols() -> None:
    assert isinstance(FixturePlaces(), PlacesProvider)
    assert isinstance(FixtureEvents(), EventsProvider)
    assert isinstance(FixtureWeather(), WeatherProvider)
    assert isinstance(FixtureGeocoder(), Geocoder)
    assert FixturePlaces().name == "fixture"


# ---------------------------------------------------------------------------
# Geocoder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["Bangalore", "bengaluru", "BLR", "  Bangalore City ", "Bangalore, India", "BENGALURU"])
async def test_geocoder_aliases_resolve_to_one_location(query: str) -> None:
    [loc] = await FixtureGeocoder().resolve(query)
    assert (loc.id, loc.timezone, loc.currency, loc.country_code) == (
        "fixture:bangalore", "Asia/Kolkata", "INR", "IN")


async def test_geocoder_other_cities_and_unknown() -> None:
    geo = FixtureGeocoder()
    assert [loc.id for loc in await geo.resolve("Bombay")] == ["fixture:mumbai"]
    [london] = await geo.resolve("london")
    assert (london.country_code, london.currency, london.timezone) == ("GB", "GBP", "Europe/London")
    assert [loc.id for loc in await geo.resolve("Jaipur")] == ["fixture:jaipur"]
    assert await geo.resolve("Atlantisville") == []
    assert await geo.resolve("") == []
    assert await geo.resolve("   ") == []


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------


def test_covers(locations: dict[str, Location]) -> None:
    places = FixturePlaces()
    assert places.covers(locations["blr"]) and places.covers(locations["bom"])
    assert not places.covers(locations["jai"])  # geocode-only city (outage case)
    assert not places.covers(locations["lon"])


async def test_jaipur_has_no_places(ctx_factory: CtxFactory, locations: dict[str, Location]) -> None:
    assert await FixturePlaces().search(PlaceQuery(intent="cafe"), ctx_factory(locations["jai"])) == []


async def test_candidates_carry_the_context_city_id(ctx_factory: CtxFactory) -> None:
    nearby = Location(id="geonames:1277333", name="Bengaluru", country_code="IN", lat=12.97194, lng=77.59369,
                      timezone="Asia/Kolkata", currency="INR")
    res = await FixturePlaces().search(PlaceQuery(intent="cafe"), ctx_factory(nearby))
    assert res and all(c.city_id == "geonames:1277333" for c in res)


async def test_assignment_cluster_queries(blr_ctx: WorldContext) -> None:
    places = FixturePlaces()
    cafes = await places.search(PlaceQuery(intent="quiet vegetarian cafe"), blr_ctx)
    assert cafes[0].id == "fx:blr-leafy-lane-cafe"
    music = await places.search(PlaceQuery(intent="quiet live music", open_at_min=17 * 60), blr_ctx)
    assert music[0].id == "fx:blr-lavelle-music-room"
    walks = await places.search(PlaceQuery(intent="walk", category="park"), blr_ctx)
    assert "fx:blr-cubbon-park" in [c.id for c in walks]
    assert all(c.category == "park" for c in walks)


async def test_neighbourhood_queries(blr_ctx: WorldContext) -> None:
    places = FixturePlaces()
    vegan = await places.search(PlaceQuery(intent="vegan cafe in Indiranagar"), blr_ctx)
    assert vegan[0].id == "fx:blr-sprout-vegan-cafe"
    books = await places.search(PlaceQuery(intent="bookshop in Indiranagar"), blr_ctx)
    assert books[0].id == "fx:blr-second-chapter-books"
    art = await places.search(PlaceQuery(intent="art gallery in Koramangala"), blr_ctx)
    assert art[0].id == "fx:blr-studio-kora-gallery"
    brunch = await places.search(PlaceQuery(intent="brunch in Koramangala", open_at_min=11 * 60), blr_ctx)
    assert {"fx:blr-hole-in-the-wall", "fx:blr-dyu-art-cafe"} <= {c.id for c in brunch}


async def test_radius_open_at_price_and_limit(blr_ctx: WorldContext) -> None:
    places = FixturePlaces()
    museums = await places.search(PlaceQuery(intent="museum"), blr_ctx)
    assert "fx:blr-indian-music-experience" not in [c.id for c in museums]  # ~8 km away
    wide = await places.search(PlaceQuery(intent="music museum", radius_m=15000), blr_ctx)
    assert "fx:blr-indian-music-experience" in [c.id for c in wide]

    at_three = await places.search(PlaceQuery(intent="vegetarian food", open_at_min=900, limit=20), blr_ctx)
    ids = {c.id for c in at_three}
    assert "fx:blr-kasturba-tiffin-room" not in ids  # closes at 15:00
    restaurants = await places.search(PlaceQuery(intent="", category="museum", open_at_min=900, limit=20), blr_ctx)
    assert "fx:blr-postal-heritage-museum" not in {c.id for c in restaurants}  # closed on Saturdays
    music = await places.search(PlaceQuery(intent="music", open_at_min=1200, radius_m=10000, limit=20), blr_ctx)
    assert "fx:blr-chowdiah-hall" in {c.id for c in music}  # unknown hours are kept (validator warns)

    free = await places.search(PlaceQuery(intent="art", max_price_level=0, limit=20), blr_ctx)
    assert free and all(c.cost.min == 0 for c in free)
    assert len(await places.search(PlaceQuery(intent="food", limit=3), blr_ctx)) == 3


async def test_search_is_deterministic(blr_ctx: WorldContext) -> None:
    q = PlaceQuery(intent="food", limit=20)
    first = await FixturePlaces().search(q, blr_ctx)
    second = await FixturePlaces().search(q, blr_ctx)
    assert [c.id for c in first] == [c.id for c in second]


async def test_city_isolation(blr_ctx: WorldContext, bom_ctx: WorldContext) -> None:
    places = FixturePlaces()
    bom = await places.search(PlaceQuery(intent="food", limit=20), bom_ctx)
    assert bom and all(c.id.startswith("fx:bom-") for c in bom)
    blr = await places.search(PlaceQuery(intent="food", limit=20), blr_ctx)
    assert blr and all(c.id.startswith("fx:blr-") for c in blr)
    assert await places.get("fx:bom-ncpa", blr_ctx) is None


async def test_get(blr_ctx: WorldContext) -> None:
    c = await FixturePlaces().get("fx:blr-cubbon-park", blr_ctx)
    assert c is not None and c.name == "Cubbon Park" and c.city_id == "fixture:bangalore"
    assert await FixturePlaces().get("fx:does-not-exist", blr_ctx) is None


async def test_hours_are_saturday_only(ctx_factory: CtxFactory) -> None:
    friday = ctx_factory(day=date(2026, 11, 6))
    c = await FixturePlaces().get("fx:blr-cubbon-park", friday)
    assert c is not None and c.hours.status == "unknown"


def test_all_candidates(blr_ctx: WorldContext) -> None:
    cands = FixturePlaces().all_candidates(blr_ctx)
    assert len(cands) >= 40 and all(c.city_id == "fixture:bangalore" for c in cands)


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


async def test_weather_scenarios(locations: dict[str, Location]) -> None:
    day = date(2026, 11, 7)
    clear = await FixtureWeather("clear").forecast(locations["blr"], day)
    assert clear.status == "ok" and clear.sunset_min == 1070 and not clear.is_rainy
    assert clear.summary == weather_summary(clear.weather_code, clear.temp_max_c, clear.precip_prob_max)
    rain = await FixtureWeather("rain_85pct").forecast(locations["blr"], day)
    assert rain.precip_prob_max == 85 and rain.is_rainy and "rain" in rain.summary.lower()
    off = await FixtureWeather("unavailable").forecast(locations["blr"], day)
    assert off.status == "unavailable" and off.summary == "Weather not available"
    mumbai = await FixtureWeather().forecast(locations["bom"], day)
    assert mumbai.sunset_min == 1085


async def test_weather_unknown_and_nearby_locations() -> None:
    far = Location(id="geonames:1", name="Atlantis", country_code="XX", lat=0.0, lng=0.0, timezone="UTC")
    assert (await FixtureWeather().forecast(far, date(2026, 11, 7))).status == "unavailable"
    near = Location(id="geonames:1277333", name="Bengaluru", country_code="IN", lat=12.97, lng=77.59,
                    timezone="Asia/Kolkata")
    assert (await FixtureWeather().forecast(near, date(2026, 11, 7))).sunset_min == 1070
    with pytest.raises(ValueError):
        FixtureWeather("snow")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def test_default_events_keep_only_the_valid_same_day_event(blr_ctx: WorldContext) -> None:
    events = await FixtureEvents().search(["music"], blr_ctx)
    assert [e.name for e in events] == ["Indie Acoustic Evening"]
    [ev] = events
    assert ev.kind == "event" and ev.category == "event" and ev.id.startswith("evfx:")
    assert ev.event_date == blr_ctx.date and (ev.event_start_min, ev.event_end_min) == (1050, 1140)
    assert ev.source.provider == "fixture_event" and ev.source.url is not None
    assert ev.source.url.startswith("https://")
    assert (ev.cost.min, ev.cost.max, ev.cost.basis) == (300, 300, "ticket_price")
    assert ev.city_id == blr_ctx.location.id


def test_all_candidates_expose_the_bad_events_for_validator_tests(blr_ctx: WorldContext) -> None:
    events = {e.name: e for e in FixtureEvents().all_candidates(blr_ctx)}
    assert set(events) == {"Indie Acoustic Evening", "Sunday Jazz Brunch", "Rooftop Unplugged Night"}
    assert events["Sunday Jazz Brunch"].event_date == date(2026, 11, 8)
    assert events["Rooftop Unplugged Night"].source.url is None


async def test_injected_event_name_is_truncated_and_description_dropped(blr_ctx: WorldContext) -> None:
    [ev] = await FixtureEvents(["jsonld_name_with_injected_instructions"]).search([], blr_ctx)
    assert len(ev.name) <= 120 and ev.name.startswith("Jazz Night.")
    dumped = ev.model_dump_json()
    assert "IGNORE PREVIOUS INSTRUCTIONS" not in dumped and "developer mode" not in dumped


async def test_events_max_price_and_other_cities(blr_ctx: WorldContext, bom_ctx: WorldContext) -> None:
    assert await FixtureEvents().search([], blr_ctx, max_price=100) == []
    assert await FixtureEvents().search([], bom_ctx) == []
    with pytest.raises(KeyError):
        FixtureEvents(["no_such_scenario"])


async def test_query_neighbourhood_centres_fixture_search(blr_ctx: WorldContext) -> None:
    places = FixturePlaces()
    vegan = await places.search(PlaceQuery(intent="vegan cafe", neighbourhood="Indiranagar"), blr_ctx)
    assert vegan[0].id == "fx:blr-sprout-vegan-cafe"
    art = await places.search(PlaceQuery(intent="art gallery", neighbourhood="koramangala"), blr_ctx)
    assert art[0].id == "fx:blr-studio-kora-gallery"
    # without the neighbourhood the Koramangala gallery is outside the default radius from the centre
    assert "fx:blr-studio-kora-gallery" not in {c.id for c in await places.search(PlaceQuery(intent="art gallery"),
                                                                                   blr_ctx)}
    unknown = await places.search(PlaceQuery(intent="cafe", neighbourhood="Nowhere Nagar"), blr_ctx)
    assert unknown and all(c.id.startswith("fx:blr-") for c in unknown)
