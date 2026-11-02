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


async def test_geocode_maps_results_to_locations() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={
            "results": [geo_result(), geo_result(id=1, name="Bengaluru Rural", timezone=None)]}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Bengaluru")
    params = route.calls.last.request.url.params
    assert (params["name"], params["count"], params["language"], params["format"]) == ("Bengaluru", "5", "en", "json")
    [loc] = res  # the malformed second result (no timezone) is dropped
    assert loc == Location(id="geonames:1277333", name="Bengaluru", country_code="IN", admin1="Karnataka",
                           lat=12.97194, lng=77.59369, timezone="Asia/Kolkata", currency="INR", population=8443675)


async def test_geocode_falls_back_to_alias_when_nothing_matches() -> None:
    with respx.mock(assert_all_called=True) as mock:
        first = mock.get(GEOCODE_URL, params={"name": "Bangalore"}).mock(
            return_value=httpx.Response(200, json={"generationtime_ms": 0.5}))
        second = mock.get(GEOCODE_URL, params={"name": "Bengaluru"}).mock(
            return_value=httpx.Response(200, json={"results": [geo_result()]}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Bangalore")
    assert first.called and second.called
    assert [r.id for r in res] == ["geonames:1277333"]


async def test_geocode_unknown_city_and_country_hint() -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL, params={"name": "Atlantisville"}).mock(return_value=httpx.Response(200, json={}))
        mock.get(GEOCODE_URL, params={"name": "London"}).mock(return_value=httpx.Response(200, json={"results": [
            geo_result(id=2643743, name="London", country_code="GB", admin1="England", timezone="Europe/London",
                       latitude=51.50853, longitude=-0.12574),
            geo_result(id=6058560, name="London", country_code="CA", admin1="Ontario", timezone="America/Toronto",
                       latitude=42.98339, longitude=-81.23304),
        ]}))
        async with httpx.AsyncClient() as client:
            geo = OpenMeteoGeocoder(client)
            assert await geo.resolve("Atlantisville") == []
            both = await geo.resolve("London")
            assert [r.currency for r in both] == ["GBP", "CAD"]
            canada = await geo.resolve("London, Ontario")
            assert [r.country_code for r in canada] == ["CA"]
            assert await geo.resolve("x") == []
            assert await geo.resolve("   ") == []


@pytest.mark.parametrize(("status", "retryable"), [(500, True), (429, True), (400, False)])
async def test_geocode_errors_raise(status: int, retryable: bool) -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await OpenMeteoGeocoder(client).resolve("Mumbai")
    assert info.value.retryable is retryable


async def test_geocode_timeout() -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await OpenMeteoGeocoder(client).resolve("Mumbai")
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
    async with httpx.AsyncClient() as client:
        [blr, *_] = await OpenMeteoGeocoder(client).resolve("Bangalore")
        assert blr.timezone == "Asia/Kolkata" and blr.currency == "INR"
        w = await OpenMeteoWeather(client).forecast(blr, date.today())
    assert w.status == "ok" and w.sunset_min is not None


@pytest.mark.parametrize("payload", [[], "nope", {"results": "nope"}, {"results": {"id": 1}}])
async def test_geocode_unexpected_shapes_raise_provider_error(payload: Any) -> None:
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderError) as info:
                await OpenMeteoGeocoder(client).resolve("Mumbai")
    assert info.value.code == "bad_response"


async def test_geocode_skips_odd_results() -> None:
    odd = [geo_result(latitude="north"), "string", geo_result(country_code=7), geo_result(id=99, name="Good")]
    with respx.mock() as mock:
        mock.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": odd}))
        async with httpx.AsyncClient() as client:
            res = await OpenMeteoGeocoder(client).resolve("Good")
    assert [r.id for r in res] == ["geonames:99"]


@pytest.mark.parametrize("payload", [[], {"daily": []}, {"daily": "x"}, {"daily": {"time": "2026-11-07"}}])
async def test_forecast_unexpected_shapes_are_unavailable(locations: dict[str, Location], payload: Any) -> None:
    with respx.mock() as mock:
        mock.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as client:
            w = await OpenMeteoWeather(client).forecast(locations["blr"], SAT)
    assert w.status == "unavailable"
