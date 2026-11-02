"""Open-Meteo provider tests with synthetic responses (respx); no network."""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from app.domain.schemas import Location
from app.providers.base import Geocoder, ProviderError, WeatherProvider
from app.providers.open_meteo import FORECAST_URL, GEOCODE_URL, OpenMeteoGeocoder, OpenMeteoWeather

SAT = date(2026, 11, 7)


def geo_result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1277333, "name": "Bengaluru", "latitude": 12.97194, "longitude": 77.59369, "elevation": 920.0,
        "feature_code": "PPLA", "country_code": "IN", "admin1": "Karnataka", "timezone": "Asia/Kolkata",
        "population": 8443675, "country": "India",
    }
    base.update(overrides)
    return base


def test_protocols() -> None:
    client = httpx.AsyncClient()
    assert isinstance(OpenMeteoGeocoder(client), Geocoder)
    assert isinstance(OpenMeteoWeather(client), WeatherProvider)


def gr(gid: int, name: str, lat: float, lng: float, cc: str, tz: str, pop: int | None, admin1: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"id": gid, "name": name, "latitude": lat, "longitude": lng, "country_code": cc,
                           "timezone": tz, "admin1": admin1}
    if pop is not None:
        out["population"] = pop
    return out


BANGALORE_TOWN_PK = gr(1183880, "Bangalore Town", 24.8817, 67.0844, "PK", "Asia/Karachi", None, "Sindh")
BENGALURU = gr(1277333, "Bengaluru", 12.97194, 77.59369, "IN", "Asia/Kolkata", 8443675, "Karnataka")
MUMBAI = gr(1275339, "Mumbai", 19.07283, 72.88261, "IN", "Asia/Kolkata", 12691836, "Maharashtra")
NEW_DELHI = gr(1261481, "New Delhi", 28.63576, 77.22445, "IN", "Asia/Kolkata", 317797, "Delhi")
DELHI = gr(1273294, "Delhi", 28.65195, 77.23149, "IN", "Asia/Kolkata", 10927986, "Delhi")
DELHI_CA = gr(5940956, "Delhi", 42.85341, -80.49971, "CA", "America/Toronto", 4000, "Ontario")


def geo_route(mock: respx.MockRouter, name: str, results: list[dict[str, Any]], country: str | None = None
              ) -> respx.Route:
    params = {"name": name, **({"countryCode": country} if country else {})}
    return mock.get(GEOCODE_URL, params=params).mock(return_value=httpx.Response(200, json={"results": results}
                                                                                if results else {}))


async def test_geocode_maps_results_to_locations() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={
            "results": [geo_result(), geo_result(id=1, name="Bengaluru Rural", timezone=None)]}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Bengaluru")
    params = route.calls.last.request.url.params
    assert (params["name"], params["count"], params["language"], params["format"]) == ("Bengaluru", "10", "en", "json")
    assert params["countryCode"] == "IN"
    [loc] = res  # the malformed second result (no timezone) is dropped
    assert loc == Location(id="geonames:1277333", name="Bengaluru", country_code="IN", admin1="Karnataka",
                           lat=12.97194, lng=77.59369, timezone="Asia/Kolkata", currency="INR", population=8443675)


async def test_bangalore_resolves_to_bengaluru_india_not_pakistan() -> None:
    """Live Open-Meteo returns only "Bangalore Town, PK" for "Bangalore"; the gazetteer steers it to India."""
    with respx.mock(assert_all_called=False) as mock:
        in_bangalore = geo_route(mock, "Bangalore", [], "IN")
        in_bengaluru = geo_route(mock, "Bengaluru", [BENGALURU], "IN")
        pk = geo_route(mock, "Bangalore", [BANGALORE_TOWN_PK])  # what a country-less query returns (live)
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Bangalore")
    assert [(r.id, r.country_code, r.currency, r.timezone) for r in res] == [
        ("geonames:1277333", "IN", "INR", "Asia/Kolkata")]
    assert in_bangalore.called and in_bengaluru.called and not pk.called


async def test_wrong_country_results_are_ignored_for_a_gazetteer_city() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": [BANGALORE_TOWN_PK]}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("bangalore")
    # nothing usable from the API (wrong country for every spelling): the gazetteer entry is returned
    assert [(r.id, r.country_code, r.timezone, r.currency) for r in res] == [
        ("gazetteer:bangalore", "IN", "Asia/Kolkata", "INR")]
    assert (res[0].lat, res[0].lng) == (12.9716, 77.5946)


async def test_bombay_resolves_to_mumbai() -> None:
    dharavi = gr(1272866, "Dharavi", 19.0402, 72.8550, "IN", "Asia/Kolkata", 1000000, "Maharashtra")
    with respx.mock(assert_all_called=True) as mock:
        geo_route(mock, "Bombay", [dharavi], "IN")  # live Open-Meteo returns nearby places, not the city
        geo_route(mock, "Mumbai", [MUMBAI], "IN")
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Bombay")
    assert [(r.name, r.country_code, r.currency) for r in res] == [("Mumbai", "IN", "INR")]


async def test_delhi_prefers_the_exact_name_and_india() -> None:
    with respx.mock(assert_all_called=False) as mock:
        geo_route(mock, "Delhi", [NEW_DELHI, DELHI_CA, DELHI], "IN")  # defensive: a CA row slipped through
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("delhi")
    assert [(r.id, r.name, r.country_code) for r in res] == [
        ("geonames:1273294", "Delhi", "IN"), ("geonames:1261481", "New Delhi", "IN")]


async def test_country_hint_overrides_the_gazetteer() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(GEOCODE_URL, params={"name": "Delhi", "countryCode": "CA"}).mock(
            return_value=httpx.Response(200, json={"results": [DELHI_CA]}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Delhi, Canada")
    assert route.called and [(r.country_code, r.currency) for r in res] == [("CA", "CAD")]


async def test_world_city_in_the_gazetteer() -> None:
    paris_fr = gr(2988507, "Paris", 48.85341, 2.3488, "FR", "Europe/Paris", 2138551, "Île-de-France")
    with respx.mock(assert_all_called=False) as mock:
        route = geo_route(mock, "Paris", [paris_fr], "FR")
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Paris, France")
    assert route.called and [(r.name, r.country_code, r.currency) for r in res] == [("Paris", "FR", "EUR")]


async def test_world_city_outside_the_gazetteer_is_ranked() -> None:
    results = [
        gr(4409896, "Springfield", 37.21533, -93.29824, "US", "America/Chicago", 166810, "Missouri"),
        gr(9999999, "Springfield Gardens", 40.6632, -73.7621, "US", "America/New_York", 900000, "New York"),
        gr(4951788, "Springfield", 42.10148, -72.58981, "US", "America/New_York", 153606, "Massachusetts"),
        gr(4250542, "Springfield", 39.80172, -89.64371, "US", "America/Chicago", 116565, "Illinois"),
    ]
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(GEOCODE_URL, params={"name": "Springfield"}).mock(
            return_value=httpx.Response(200, json={"results": list(reversed(results))}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Springfield")
            massachusetts = await OpenMeteoGeocoder(client).resolve("Springfield, Massachusetts")
    assert "countryCode" not in route.calls[0].request.url.params
    assert [r.admin1 for r in res] == ["Missouri", "Massachusetts", "Illinois", "New York"]  # exact, then population
    assert [r.admin1 for r in massachusetts] == ["Massachusetts"]


async def test_unknown_city() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(GEOCODE_URL, params={"name": "Atlantisville"}).mock(return_value=httpx.Response(200, json={}))
        async with httpx.AsyncClient() as client:
            geo = OpenMeteoGeocoder(client)
            assert await geo.resolve("Atlantisville") == []
            assert await geo.resolve("x") == []
            assert await geo.resolve("   ") == []


async def test_non_gazetteer_alias_fallback() -> None:
    with respx.mock(assert_all_called=True) as mock:
        first = mock.get(GEOCODE_URL, params={"name": "Belgaum"}).mock(return_value=httpx.Response(200, json={}))
        second = mock.get(GEOCODE_URL, params={"name": "Belagavi"}).mock(return_value=httpx.Response(200, json={
            "results": [gr(1276533, "Belagavi", 15.85212, 74.50447, "IN", "Asia/Kolkata", 488157, "Karnataka")]}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Belgaum")
    assert first.called and second.called and [r.name for r in res] == ["Belagavi"]


async def test_gazetteer_city_survives_a_geocoder_outage() -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Mumbai")
    assert [(r.id, r.country_code, r.timezone) for r in res] == [("gazetteer:mumbai", "IN", "Asia/Kolkata")]


@pytest.mark.parametrize(("status", "retryable"), [(500, True), (429, True), (400, False)])
async def test_geocode_errors_raise(status: int, retryable: bool) -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await OpenMeteoGeocoder(client).resolve("Springfield")
    assert info.value.retryable is retryable


async def test_geocode_timeout() -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await OpenMeteoGeocoder(client).resolve("Springfield")
    assert info.value.code == "timeout" and info.value.retryable


def forecast_body(**daily_overrides: Any) -> dict[str, Any]:
    daily: dict[str, Any] = {
        "time": ["2026-11-07"], "weather_code": [63], "temperature_2m_max": [23.6], "temperature_2m_min": [18.1],
        "precipitation_probability_max": [85], "sunrise": ["2026-11-07T06:13"], "sunset": ["2026-11-07T17:50"],
    }
    daily.update(daily_overrides)
    return {"latitude": 12.97, "longitude": 77.59, "timezone": "Asia/Kolkata", "daily": daily}


async def test_forecast_parses_daily_values(locations: dict[str, Location]) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_body()))
        async with httpx.AsyncClient() as client:
            w = await OpenMeteoWeather(client).forecast(locations["blr"], SAT)
    params = route.calls.last.request.url.params
    assert params["daily"] == ("weather_code,temperature_2m_max,temperature_2m_min,"
                               "precipitation_probability_max,sunrise,sunset")
    assert (params["timezone"], params["start_date"], params["end_date"]) == ("auto", "2026-11-07", "2026-11-07")
    assert (params["latitude"], params["longitude"]) == ("12.9716", "77.5946")
    assert w.status == "ok" and w.is_rainy
    assert (w.weather_code, w.precip_prob_max, w.temp_max_c, w.temp_min_c) == (63, 85, 23.6, 18.1)
    assert (w.sunrise_min, w.sunset_min) == (373, 1070)
    assert w.summary == "Moderate rain, high 24°C, 85% chance of rain"


async def test_forecast_tolerates_null_fields(locations: dict[str, Location]) -> None:
    body = forecast_body(precipitation_probability_max=[None], weather_code=[0], sunrise=[None])
    with respx.mock() as mock:
        mock.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as client:
            w = await OpenMeteoWeather(client).forecast(locations["blr"], SAT)
    assert w.status == "ok" and w.precip_prob_max is None and w.sunrise_min is None and w.sunset_min == 1070
    assert w.summary == "Clear sky, high 24°C"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": True, "reason": "start_date out of range"}),
        httpx.Response(503),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"daily": {}}),
        httpx.Response(200, json=forecast_body(time=["2026-11-08"])),
        httpx.Response(200, json=forecast_body(weather_code=[None], temperature_2m_max=[None],
                                               precipitation_probability_max=[None])),
        httpx.Response(200, json=[]),
    ],
)
async def test_forecast_failures_are_unavailable(locations: dict[str, Location], response: httpx.Response) -> None:
    with respx.mock() as mock:
        mock.get(FORECAST_URL).mock(return_value=response)
        async with httpx.AsyncClient() as client:
            w = await OpenMeteoWeather(client).forecast(locations["blr"], SAT)
    assert w.status == "unavailable" and w.summary == "Weather not available"


async def test_forecast_network_error_is_unavailable(locations: dict[str, Location]) -> None:
    with respx.mock() as mock:
        mock.get(FORECAST_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        async with httpx.AsyncClient() as client:
            w = await OpenMeteoWeather(client).forecast(locations["blr"], SAT)
    assert w.status == "unavailable"


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="needs RUN_LIVE=1 and network")
async def test_live_open_meteo() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "SaturdayPlanner/0.1"}) as client:
        [blr, *_] = await OpenMeteoGeocoder(client).resolve("Bangalore")
        assert blr.country_code == "IN" and blr.timezone == "Asia/Kolkata" and blr.currency == "INR"
        assert abs(blr.lat - 12.97) < 0.3 and abs(blr.lng - 77.59) < 0.3
        w = await OpenMeteoWeather(client).forecast(blr, date.today())
    assert w.status == "ok" and w.sunset_min is not None


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="needs RUN_LIVE=1 and network")
async def test_live_bangalore_resolves_to_india() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "SaturdayPlanner/0.1"}) as client:
        geo = OpenMeteoGeocoder(client)
        [blr, *_] = await geo.resolve("Bangalore")
        [bom, *_] = await geo.resolve("Bombay")
    assert (blr.country_code, blr.currency) == ("IN", "INR") and blr.id.startswith(("geonames:", "gazetteer:"))
    assert (bom.country_code, bom.currency) == ("IN", "INR")


@pytest.mark.parametrize("payload", [[], "nope", {"results": "nope"}, {"results": {"id": 1}}])
async def test_geocode_unexpected_shapes_raise_provider_error(payload: Any) -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await OpenMeteoGeocoder(client).resolve("Springfield")
    assert info.value.code == "bad_response"


async def test_geocode_skips_odd_results() -> None:
    odd = [geo_result(latitude="north"), "string", geo_result(country_code=7),
           geo_result(id=99, name="Goodtown", country_code="US", timezone="America/Chicago")]
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": odd}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Goodtown")
    assert [r.id for r in res] == ["geonames:99"]


@pytest.mark.parametrize("payload", [[], {"daily": []}, {"daily": "x"}, {"daily": {"time": "2026-11-07"}}])
async def test_forecast_unexpected_shapes_are_unavailable(locations: dict[str, Location], payload: Any) -> None:
    with respx.mock() as mock:
        mock.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            w = await OpenMeteoWeather(client).forecast(locations["blr"], SAT)
    assert w.status == "unavailable"


async def test_nearby_places_with_other_names_fall_back_to_the_gazetteer() -> None:
    sion = gr(1256113, "Sion", 19.0390, 72.8619, "IN", "Asia/Kolkata", 50000, "Maharashtra")
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": [sion]}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Bombay")
    assert [(r.id, r.name) for r in res] == [("gazetteer:mumbai", "Mumbai")]
