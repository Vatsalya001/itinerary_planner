"""GooglePlaces tests. Every response below is SYNTHETIC JSON written for these tests (shape per the public
Places API (New) reference); nothing is copied from real Google responses."""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from app.domain.schemas import Evidence, Location, PlaceQuery, WorldContext
from app.providers.base import PlacesProvider, ProviderError
from app.providers.google_places import (
    DETAILS_FIELD_MASK,
    SEARCH_FIELD_MASK,
    SEARCH_URL,
    GooglePlaces,
    google_hours_for_date,
    text_query,
)
from app.providers.normalize import ALWAYS_OPEN

KEY = "test-key-SECRET-SENTINEL-123"
SAT = 6  # Google weekday for Saturday


def _p(day: int, hour: int, minute: int = 0) -> dict[str, int]:
    return {"day": day, "hour": hour, "minute": minute}


def place(pid: str = "PLACE_A", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": pid,
        "displayName": {"text": f"Synthetic {pid}", "languageCode": "en"},
        "formattedAddress": "1 Test Road, Bengaluru",
        "location": {"latitude": 12.9721, "longitude": 77.5952},
        "types": ["cafe", "food", "point_of_interest", "establishment"],
        "primaryType": "cafe",
        "googleMapsUri": f"https://maps.google.com/?cid={pid}",
        "businessStatus": "OPERATIONAL",
    }
    base.update(overrides)
    return base


async def run_search(places: list[dict[str, Any]], ctx: WorldContext, query: PlaceQuery | None = None
                     ) -> tuple[list[Any], respx.Route]:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"places": places}))
        async with httpx.AsyncClient() as client:
            res = await GooglePlaces(KEY, client).search(query or PlaceQuery(intent="quiet cafe"), ctx)
    return res, route


def test_protocol_and_repr() -> None:
    gp = GooglePlaces(KEY, httpx.AsyncClient())
    assert isinstance(gp, PlacesProvider) and gp.name == "google_places"
    assert KEY not in repr(gp)
    assert gp.covers(Location(id="x", name="Anywhere", country_code="ZZ", lat=0, lng=0, timezone="UTC"))


async def test_search_request_shape(blr_ctx: WorldContext) -> None:
    _, route = await run_search([], blr_ctx, PlaceQuery(intent="quiet vegetarian cafe", limit=15, radius_m=3000))
    req = route.calls.last.request
    assert req.headers["X-Goog-Api-Key"] == KEY
    assert req.headers["X-Goog-FieldMask"] == SEARCH_FIELD_MASK
    for field in ("places.id", "places.priceRange", "places.servesVegetarianFood", "places.currentOpeningHours",
                  "places.accessibilityOptions", "places.primaryType", "places.googleMapsUri"):
        assert field in SEARCH_FIELD_MASK.split(",")
    body = json.loads(req.content)
    assert body["textQuery"] == "quiet vegetarian cafe in Bengaluru"
    assert body["pageSize"] == 10
    assert body["locationBias"] == {"circle": {"center": {"latitude": 12.9716, "longitude": 77.5946}, "radius": 3000.0}}
    assert body["languageCode"] == "en" and body["regionCode"] == "IN"


async def test_location_bias_uses_near_point(blr_ctx: WorldContext) -> None:
    _, route = await run_search([], blr_ctx, PlaceQuery(intent="cafe", near_lat=12.93, near_lng=77.62, limit=3))
    body = json.loads(route.calls.last.request.content)
    assert body["locationBias"]["circle"]["center"] == {"latitude": 12.93, "longitude": 77.62}
    assert body["pageSize"] == 3


async def test_normalizes_a_rich_cafe(blr_ctx: WorldContext) -> None:
    p = place(
        priceRange={"startPrice": {"currencyCode": "INR", "units": "200"},
                    "endPrice": {"currencyCode": "INR", "units": "400"}},
        priceLevel="PRICE_LEVEL_MODERATE",
        servesVegetarianFood=True,
        accessibilityOptions={"wheelchairAccessibleEntrance": True},
        rating=4.4, userRatingCount=1200, liveMusic=False, outdoorSeating=True,
        regularOpeningHours={"periods": [{"open": _p(SAT, 8), "close": _p(SAT, 22)}]},
    )
    [c], _ = await run_search([p], blr_ctx)
    assert c.id == "gp:PLACE_A" and c.name == "Synthetic PLACE_A" and c.category == "cafe"
    assert c.city_id == "fixture:bangalore"
    assert (c.cost.min, c.cost.max, c.cost.currency, c.cost.basis) == (200, 400, "INR", "listed_price_range")
    assert c.diet == {"vegetarian": Evidence.CONFIRMED, "vegan": Evidence.UNKNOWN, "jain": Evidence.UNKNOWN}
    assert c.wheelchair == Evidence.CONFIRMED
    assert c.hours.status == "known" and c.hours.periods == [(480, 1320)]
    assert c.popularity == "medium" and c.busy_windows  # medium keeps category windows
    assert c.rating == 4.4 and c.rating_count == 1200
    assert c.live_music is False and "outdoor seating" in c.tags
    assert c.indoor is True and c.seated is True
    assert c.maps_url == "https://maps.google.com/?cid=PLACE_A"
    assert c.source.provider == "google_places" and c.source.attribution == "Google Maps"
    assert c.source.retrieved_at is not None


async def test_vegan_type_overnight_hours_and_price_level(blr_ctx: WorldContext) -> None:
    p = place("VEG", types=["vegan_restaurant", "restaurant", "food"], primaryType="vegan_restaurant",
              priceLevel="PRICE_LEVEL_MODERATE",
              regularOpeningHours={"periods": [
                  {"open": _p(5, 18), "close": _p(SAT, 1)},       # Friday night spills into Saturday
                  {"open": _p(SAT, 18), "close": _p(0, 2)},       # Saturday 18:00 -> Sunday 02:00
              ]})
    [c], _ = await run_search([p], blr_ctx)
    assert c.category == "restaurant"
    assert c.diet["vegan"] == Evidence.CONFIRMED and c.diet["vegetarian"] == Evidence.CONFIRMED
    assert (c.cost.min, c.cost.max, c.cost.basis) == (400, 1000, "price_level_estimate")
    assert c.hours.periods == [(0, 60), (1080, 1560)]


async def test_vegetarian_type_and_false_flag(blr_ctx: WorldContext) -> None:
    veg = place("V1", types=["vegetarian_restaurant", "restaurant"], primaryType="vegetarian_restaurant")
    no = place("V2", types=["restaurant"], primaryType="restaurant", servesVegetarianFood=False)
    unknown = place("V3", types=["restaurant"], primaryType="restaurant")
    res, _ = await run_search([veg, no, unknown], blr_ctx)
    by_id = {c.id: c for c in res}
    assert by_id["gp:V1"].diet["vegetarian"] == Evidence.CONFIRMED
    assert by_id["gp:V2"].diet["vegetarian"] == Evidence.NO
    assert by_id["gp:V3"].diet["vegetarian"] == Evidence.UNKNOWN
    assert by_id["gp:V3"].hours.status == "unknown"


async def test_drops_closed_far_unnamed_and_unmapped(blr_ctx: WorldContext) -> None:
    places = [
        place("CLOSED", businessStatus="CLOSED_PERMANENTLY"),
        place("TEMP", businessStatus="CLOSED_TEMPORARILY"),
        place("FAR", location={"latitude": 18.93, "longitude": 72.83}),
        place("NOLOC", location={}),
        place("NONAME", displayName={}),
        place("ODD", types=["point_of_interest", "establishment"], primaryType="point_of_interest"),
        place("OK"),
    ]
    res, _ = await run_search(places, blr_ctx)
    assert [c.id for c in res] == ["gp:OK"]


async def test_museum_defaults(blr_ctx: WorldContext) -> None:
    p = place("MUS", types=["museum", "tourist_attraction"], primaryType="museum",
              accessibilityOptions={"wheelchairAccessibleEntrance": False}, userRatingCount=12000)
    [c], _ = await run_search([p], blr_ctx, PlaceQuery(intent="museum"))
    assert c.category == "museum" and c.diet == {}
    assert (c.cost.min, c.cost.max, c.cost.basis) == (20, 300, "category_estimate")
    assert c.wheelchair == Evidence.NO
    assert c.popularity == "high" and c.busy_windows == [(720, 960)]
    assert c.hours.status == "unknown"


async def test_always_open_park(blr_ctx: WorldContext) -> None:
    p = place("PARK", types=["park"], primaryType="park", userRatingCount=100,
              regularOpeningHours={"periods": [{"open": _p(0, 0)}]})
    [c], _ = await run_search([p], blr_ctx, PlaceQuery(intent="park"))
    assert c.hours.periods == [ALWAYS_OPEN]
    assert (c.cost.min, c.cost.max, c.cost.basis) == (0, 0, "free")
    assert c.popularity == "low" and c.busy_windows == []
    assert c.indoor is False


async def test_current_hours_override_regular_hours(blr_ctx: WorldContext) -> None:
    regular = {"periods": [{"open": _p(d, 9), "close": _p(d, 21)} for d in range(7)]}
    # Special day: closed on Saturday 2026-11-07 even though regular hours say open.
    current = {"periods": [
        {"open": {**_p(5, 9), "date": {"year": 2026, "month": 11, "day": 6}},
         "close": {**_p(5, 21), "date": {"year": 2026, "month": 11, "day": 6}}},
        {"open": {**_p(0, 9), "date": {"year": 2026, "month": 11, "day": 8}},
         "close": {**_p(0, 21), "date": {"year": 2026, "month": 11, "day": 8}}},
    ]}
    p = place("SPECIAL", regularOpeningHours=regular, currentOpeningHours=current)
    [c], _ = await run_search([p], blr_ctx)
    assert c.hours.status == "known" and c.hours.periods == []


def test_hours_helper_edge_cases() -> None:
    sat = date(2026, 11, 7)
    regular = {"periods": [{"open": _p(1, 9), "close": _p(1, 17)}]}  # Mondays only
    assert google_hours_for_date(regular, None, sat).periods == []
    assert google_hours_for_date(None, None, sat).status == "unknown"
    assert google_hours_for_date({"periods": []}, None, sat).status == "unknown"
    stale_current = {"periods": [{"open": {**_p(3, 9), "date": {"year": 2026, "month": 10, "day": 28}},
                                  "close": {**_p(3, 17), "date": {"year": 2026, "month": 10, "day": 28}}}]}
    sat_regular = {"periods": [{"open": _p(SAT, 10), "close": _p(SAT, 12)}]}
    assert google_hours_for_date(sat_regular, stale_current, sat).periods == [(600, 720)]
    overnight_current = {"periods": [{"open": {**_p(SAT, 20), "date": {"year": 2026, "month": 11, "day": 7}},
                                      "close": {**_p(0, 3), "date": {"year": 2026, "month": 11, "day": 8}}}]}
    assert google_hours_for_date(None, overnight_current, sat).periods == [(1200, 1620)]


async def test_price_range_edge_cases(blr_ctx: WorldContext) -> None:
    open_ended = place("OPEN", priceLevel="PRICE_LEVEL_EXPENSIVE",
                       priceRange={"startPrice": {"currencyCode": "INR", "units": "1500"}})
    foreign = place("USD", priceLevel="PRICE_LEVEL_INEXPENSIVE",
                    priceRange={"startPrice": {"currencyCode": "USD", "units": "10"},
                                "endPrice": {"currencyCode": "USD", "units": "20"}})
    res, _ = await run_search([open_ended, foreign], blr_ctx)
    by_id = {c.id: c for c in res}
    oe = by_id["gp:OPEN"].cost
    assert (oe.min, oe.max, oe.basis) == (1500, 2000, "listed_price_range")
    fx = by_id["gp:USD"].cost
    assert (fx.min, fx.max, fx.currency, fx.basis) == (100, 400, "INR", "price_level_estimate")


async def test_category_filter(blr_ctx: WorldContext) -> None:
    cafe = place("C", types=["cafe"], primaryType="cafe")
    resto_with_cafe = place("RC", types=["restaurant", "cafe"], primaryType="restaurant")
    bar = place("B", types=["bar"], primaryType="bar")
    unmapped = place("U", types=["point_of_interest"], primaryType="point_of_interest")
    res, _ = await run_search([cafe, resto_with_cafe, bar, unmapped], blr_ctx, PlaceQuery(intent="coffee", category="cafe"))
    assert [(c.id, c.category) for c in res] == [("gp:C", "cafe"), ("gp:RC", "cafe"), ("gp:U", "cafe")]
    res, _ = await run_search([cafe, bar], blr_ctx, PlaceQuery(intent="drinks", category="drinks"))
    assert [c.id for c in res] == ["gp:B"]


async def test_open_at_and_price_filters(blr_ctx: WorldContext) -> None:
    morning = place("AM", regularOpeningHours={"periods": [{"open": _p(SAT, 7), "close": _p(SAT, 12)}]})
    evening = place("PM", regularOpeningHours={"periods": [{"open": _p(SAT, 14), "close": _p(SAT, 23)}]})
    unknown = place("UNK")
    res, _ = await run_search([morning, evening, unknown], blr_ctx, PlaceQuery(intent="cafe", open_at_min=900))
    assert [c.id for c in res] == ["gp:PM", "gp:UNK"]
    pricey = place("EXP", priceLevel="PRICE_LEVEL_EXPENSIVE")
    cheap = place("INX", priceLevel="PRICE_LEVEL_INEXPENSIVE")
    ranged = place("RNG", priceRange={"startPrice": {"currencyCode": "INR", "units": "1200"},
                                      "endPrice": {"currencyCode": "INR", "units": "1500"}})
    res, _ = await run_search([pricey, cheap, ranged], blr_ctx, PlaceQuery(intent="cafe", max_price_level=1))
    assert [c.id for c in res] == ["gp:INX"]


async def test_limit_is_respected(blr_ctx: WorldContext) -> None:
    res, _ = await run_search([place(f"P{i}") for i in range(10)], blr_ctx, PlaceQuery(intent="cafe", limit=4))
    assert len(res) == 4


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(429, "rate_limited", True), (503, "http_503", True), (500, "http_500", True), (403, "forbidden", False),
     (400, "bad_request", False)],
)
async def test_http_errors(blr_ctx: WorldContext, status: int, code: str, retryable: bool) -> None:
    with respx.mock() as mock:
        mock.post(SEARCH_URL).mock(return_value=httpx.Response(status, json={"error": {"message": "synthetic"}}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await GooglePlaces(KEY, client).search(PlaceQuery(intent="cafe"), blr_ctx)
    err = info.value
    assert (err.provider, err.code, err.retryable) == ("google_places", code, retryable)
    assert KEY not in str(err)


async def test_timeout_and_network_errors(blr_ctx: WorldContext) -> None:
    for exc, code in ((httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("down"), "network")):
        with respx.mock() as mock:
            mock.post(SEARCH_URL).mock(side_effect=exc)
            async with httpx.AsyncClient() as client:
                with pytest.raises(ProviderError) as info:
                    await GooglePlaces(KEY, client).search(PlaceQuery(intent="cafe"), blr_ctx)
        assert info.value.code == code and info.value.retryable
        assert KEY not in str(info.value)


async def test_get_uses_pro_field_mask(blr_ctx: WorldContext) -> None:
    detail = {k: v for k, v in place("DETAIL").items()}
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://places.googleapis.com/v1/places/DETAIL").mock(
            return_value=httpx.Response(200, json=detail))
        missing = mock.get("https://places.googleapis.com/v1/places/GONE").mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            gp = GooglePlaces(KEY, client)
            c = await gp.get("gp:DETAIL", blr_ctx)
            assert await gp.get("gp:GONE", blr_ctx) is None
            assert await gp.get("fx:blr-cubbon-park", blr_ctx) is None
            assert await gp.get("gp:../../etc", blr_ctx) is None
    assert c is not None and c.id == "gp:DETAIL" and c.category == "cafe"
    req = route.calls.last.request
    assert req.headers["X-Goog-FieldMask"] == DETAILS_FIELD_MASK and req.headers["X-Goog-Api-Key"] == KEY
    assert missing.called


def test_text_query() -> None:
    blr = Location(id="x", name="Bengaluru", country_code="IN", lat=12.97, lng=77.59, timezone="Asia/Kolkata")
    assert text_query("quiet cafe", blr) == "quiet cafe in Bengaluru"
    assert text_query("vegan cafe in Indiranagar", blr) == "vegan cafe in Indiranagar, Bengaluru"
    assert text_query("museums in bengaluru", blr) == "museums in bengaluru"
    assert text_query("  live   music ", blr) == "live music in Bengaluru"


@pytest.mark.live
@pytest.mark.skipif(not (os.environ.get("RUN_LIVE") and os.environ.get("GOOGLE_MAPS_API_KEY")),
                    reason="needs RUN_LIVE=1 and GOOGLE_MAPS_API_KEY")
async def test_live_google_places_search(blr_ctx: WorldContext) -> None:
    async with httpx.AsyncClient() as client:
        res = await GooglePlaces(os.environ["GOOGLE_MAPS_API_KEY"], client).search(
            PlaceQuery(intent="vegetarian cafe", limit=5), blr_ctx)
    assert res and all(c.id.startswith("gp:") and c.source.attribution == "Google Maps" for c in res)


# ---------------------------------------------------------------------------
# Verification fixes: D-18 malformed places, D-19 midnight join, neighbourhood bias
# ---------------------------------------------------------------------------

MALFORMED = {
    "non_string_type": place("BAD", types=[1, "cafe"], primaryType=None),
    "null_hour": place("BAD", regularOpeningHours={"periods": [{"open": {"day": SAT, "hour": None},
                                                                "close": _p(SAT, 20)}]}),
    "day_as_text": place("BAD", regularOpeningHours={"periods": [{"open": {"day": "SAT", "hour": 9},
                                                                  "close": _p(SAT, 20)}]}),
    "hour_out_of_range": place("BAD", regularOpeningHours={"periods": [{"open": _p(SAT, 30), "close": _p(SAT, 20)}]}),
    "negative_price": place("BAD", priceRange={"startPrice": {"currencyCode": "INR", "units": "-500"},
                                               "endPrice": {"currencyCode": "INR", "units": "-100"}}),
    "inverted_price": place("BAD", priceRange={"startPrice": {"currencyCode": "INR", "units": "900"},
                                               "endPrice": {"currencyCode": "INR", "units": "100"}}),
    "price_units_text": place("BAD", priceRange={"startPrice": {"currencyCode": "INR", "units": "lots"}}),
    "rating_count_text": place("BAD", userRatingCount="many"),
    "veg_flag_text": place("BAD", servesVegetarianFood="yes"),
    "periods_not_list": place("BAD", regularOpeningHours={"periods": "always"}),
    "not_an_object": "just a string",
}


@pytest.mark.parametrize("bad", list(MALFORMED.values()), ids=list(MALFORMED))
async def test_one_malformed_place_is_skipped_and_logged_by_index(
    blr_ctx: WorldContext, bad: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING", logger="app.providers.google_places")
    res, _ = await run_search([place("GOOD"), bad, place("ALSO_GOOD")], blr_ctx)
    assert [c.id for c in res] == ["gp:GOOD", "gp:ALSO_GOOD"]
    messages = [r.getMessage() for r in caplog.records]
    assert messages == ["google_places: skipped malformed place at index 1"]
    assert all("BAD" not in m and "Synthetic" not in m for m in messages)  # index only, no place content


async def test_missing_or_unusable_location_is_skipped_quietly(blr_ctx: WorldContext) -> None:
    places = [place("NOLOC", location=None), place("STRLOC", location={"latitude": "12.9", "longitude": "77.5"}),
              place("OFFMAP", location={"latitude": 123.0, "longitude": 77.5}), place("GOOD")]
    res, _ = await run_search(places, blr_ctx)
    assert [c.id for c in res] == ["gp:GOOD"]


@pytest.mark.parametrize("payload", [[], "nope", {"places": "nope"}, {"places": {"id": "x"}}])
async def test_unexpected_response_shapes_raise_provider_error(blr_ctx: WorldContext, payload: Any) -> None:
    with respx.mock() as mock:
        mock.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await GooglePlaces(KEY, client).search(PlaceQuery(intent="cafe"), blr_ctx)
    assert info.value.code == "bad_response" and not info.value.retryable


async def test_empty_response_is_no_results(blr_ctx: WorldContext) -> None:
    res, _ = await run_search([], blr_ctx)
    assert res == []
    with respx.mock() as mock:
        mock.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={}))
        async with httpx.AsyncClient() as client:
            assert await GooglePlaces(KEY, client).search(PlaceQuery(intent="cafe"), blr_ctx) == []


async def test_malformed_details_raise_provider_error(blr_ctx: WorldContext) -> None:
    with respx.mock() as mock:
        mock.get("https://places.googleapis.com/v1/places/ODDONE").mock(
            return_value=httpx.Response(200, json=place("ODDONE", types=[1])))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await GooglePlaces(KEY, client).get("gp:ODDONE", blr_ctx)
    assert info.value.code == "bad_response"


def test_per_day_24_hour_periods_are_joined_across_midnight() -> None:
    sat = date(2026, 11, 7)
    every_day = {"periods": [{"open": _p(d, 0), "close": _p((d + 1) % 7, 0)} for d in range(7)]}
    hours = google_hours_for_date(every_day, None, sat)
    assert hours.periods == [(0, 2880)]
    sat_only = {"periods": [{"open": _p(SAT, 9), "close": _p(0, 0)}]}  # Saturday 09:00 to midnight, Sunday shut
    assert google_hours_for_date(sat_only, None, sat).periods == [(540, 1440)]
    late = {"periods": [{"open": _p(SAT, 18), "close": _p(0, 0)}, {"open": _p(0, 0), "close": _p(0, 2)}]}
    assert google_hours_for_date(late, None, sat).periods == [(1080, 1560)]


def test_current_hours_are_joined_across_midnight() -> None:
    sat = date(2026, 11, 7)

    def dated(d: int, h: int, day: int) -> dict[str, Any]:
        return {**_p(d, h), "date": {"year": 2026, "month": 11, "day": day}}

    current = {"periods": [
        {"open": dated(SAT, 20, 7), "close": dated(0, 0, 8)},
        {"open": dated(0, 0, 8), "close": dated(0, 3, 8)},
    ]}
    assert google_hours_for_date(None, current, sat).periods == [(1200, 1620)]


async def test_neighbourhood_shapes_text_query_and_bias(blr_ctx: WorldContext) -> None:
    _, route = await run_search([], blr_ctx, PlaceQuery(intent="vegan cafe", neighbourhood="Indiranagar"))
    body = json.loads(route.calls.last.request.content)
    assert body["textQuery"] == "vegan cafe in Indiranagar, Bengaluru"
    assert body["locationBias"]["circle"]["center"] == {"latitude": 12.9784, "longitude": 77.6408}  # gazetteer
    _, route = await run_search([], blr_ctx, PlaceQuery(intent="vegan cafe", neighbourhood="Indiranagar",
                                                        near_lat=12.97, near_lng=77.6))
    body = json.loads(route.calls.last.request.content)
    assert body["locationBias"]["circle"]["center"] == {"latitude": 12.97, "longitude": 77.6}
    _, route = await run_search([], blr_ctx, PlaceQuery(intent="cafe", neighbourhood="Nowhere Nagar"))
    body = json.loads(route.calls.last.request.content)
    assert body["textQuery"] == "cafe in Nowhere Nagar, Bengaluru"
    assert body["locationBias"]["circle"]["center"] == {"latitude": 12.9716, "longitude": 77.5946}


def test_text_query_with_neighbourhood() -> None:
    blr = Location(id="x", name="Bengaluru", country_code="IN", lat=12.97, lng=77.59, timezone="Asia/Kolkata")
    assert text_query("quiet cafe", blr, "Koramangala") == "quiet cafe in Koramangala, Bengaluru"
    assert text_query("cafe in Koramangala", blr, "Koramangala") == "cafe in Koramangala, Bengaluru"
    assert text_query("quiet cafe", blr, None) == "quiet cafe in Bengaluru"
    assert text_query("quiet cafe", blr, "  ") == "quiet cafe in Bengaluru"
