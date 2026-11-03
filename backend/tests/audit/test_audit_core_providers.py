"""Independent audit (core): pipeline context/search steps and provider normalisation.

``xfail(strict=True)`` = confirmed bug (docs/verification/AUDIT_CORE.md, AUD-<n>). Offline only: HTTP is mocked with
respx, no real network or keys.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from app.baseline import run_baseline
from app.deps import Deps, build_deps, fixture_settings
from app.domain.categories import currency_for_country
from app.domain.pacing import profile_for
from app.domain.schemas import Energy, Location, PlaceQuery, PlanRequest, Weather, WorldContext
from app.domain.travel import HeuristicTravel
from app.pipeline import search_places
from app.providers.fixture import FixtureGeocoder, FixtureWeather
from app.providers.google_places import SEARCH_URL, GooglePlaces, _cost, place_to_candidate
from app.providers.snapshot import SnapshotPlaces
from app.trace import Tracer

SATURDAY = date(2026, 11, 7)


def ctx_for(loc: Location) -> WorldContext:
    return WorldContext(location=loc, date=SATURDAY, start_min=900, end_min=1140, weather=Weather(status="unavailable"),
                        pacing=profile_for(Energy.MEDIUM), now=datetime(2026, 11, 5, tzinfo=UTC))


def google_place(pid: str, lat: float, lng: float, currency: str = "EUR") -> dict[str, Any]:
    return {"id": pid, "displayName": {"text": f"Cafe {pid}"}, "location": {"latitude": lat, "longitude": lng},
            "types": ["cafe"], "primaryType": "cafe", "businessStatus": "OPERATIONAL",
            "priceLevel": "PRICE_LEVEL_MODERATE",
            "priceRange": {"startPrice": {"currencyCode": currency, "units": "8"},
                           "endPrice": {"currencyCode": currency, "units": "15"}}}


# ---------------------------------------------------------------------------------------------------
# AUD-9 (P1): same-day request whose start has passed: the window is shifted, so the plan runs past its end
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("request_data", [
    {"city": "Bangalore", "budget": 1500, "start_time": "16:00", "available_time": 240, "interests": ["coffee"]},
    {"free_text": "Bangalore, free 4pm to 8pm today, 1500 rupees, love coffee"},
])
async def test_aud9_late_same_day_plan_keeps_the_stated_end(request_data: dict[str, Any]) -> None:
    deps = build_deps(fixture_settings(fixed_now=datetime(2026, 11, 7, 12, 45, tzinfo=UTC)))  # Saturday 18:15 IST
    result = await run_baseline(PlanRequest(**request_data), deps)
    assert result.plan is not None and result.plan.date == SATURDAY
    assert result.plan.end_min <= 20 * 60, "the user is free until 8 PM"


# ---------------------------------------------------------------------------------------------------
# AUD-10 (P1): one failed live query (e.g. a 429) throws away every other live result
# ---------------------------------------------------------------------------------------------------

async def test_aud10_partial_google_failure_keeps_good_results() -> None:
    mumbai = Location(id="geonames:1275339", name="Mumbai", country_code="IN", lat=18.94, lng=72.83,
                      timezone="Asia/Kolkata", currency="INR")
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json={"places": [google_place(f"p{calls['n']}", 18.94, 72.83, "INR")]})

    with respx.mock(assert_all_called=False) as router:
        router.post(SEARCH_URL).mock(side_effect=responder)
        async with httpx.AsyncClient() as client:
            deps = Deps(settings=fixture_settings(), places=GooglePlaces("test-key", client), snapshot=SnapshotPlaces(),
                        events=None, geocoder=FixtureGeocoder(), weather=FixtureWeather(), travel=HeuristicTravel(),
                        clock=lambda: datetime(2026, 11, 5, tzinfo=UTC))
            queries = [PlaceQuery(intent=f"cafe {i}", category="cafe") for i in range(4)]
            candidates, _degradations, ok = await search_places(queries, ctx_for(mumbai), deps, Tracer("audit"))
    assert ok and len(candidates) == 3, f"{len(candidates)} candidates kept after 3 of 4 live queries succeeded"


# ---------------------------------------------------------------------------------------------------
# AUD-11 (P1): cities in countries missing from countries.yaml are priced in USD, listed local prices dropped
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("cc", "name", "lat", "lng"), [("BE", "Brussels", 50.85, 4.35), ("GR", "Athens", 37.98, 23.73),
                                                        ("FI", "Helsinki", 60.17, 24.94)])
def test_aud11_euro_area_city_keeps_listed_euro_price(cc: str, name: str, lat: float, lng: float) -> None:
    loc = Location(id=f"geonames:{name}", name=name, country_code=cc, lat=lat, lng=lng, timezone="Europe/Brussels",
                   currency=currency_for_country(cc))
    cand = place_to_candidate(google_place("x", lat, lng, "EUR"), ctx_for(loc), "cafe")
    assert cand is not None
    assert (cand.cost.currency, cand.cost.min, cand.cost.max) == ("EUR", 8, 15), cand.cost


# ---------------------------------------------------------------------------------------------------
# AUD-12 (P1): an open-ended priceRange ("£40+", "₹2,000+") is costed with max == min
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("place", "key", "currency"), [
    ({"priceRange": {"startPrice": {"currencyCode": "GBP", "units": "40"}}, "priceLevel": "PRICE_LEVEL_MODERATE"},
     "restaurant", "GBP"),
    ({"priceRange": {"startPrice": {"currencyCode": "INR", "units": "2000"}}}, "restaurant", "INR"),
])
def test_aud12_open_ended_price_range_has_headroom(place: dict[str, Any], key: str, currency: str) -> None:
    cost = _cost(place, key, currency)
    assert cost.max > cost.min, f"'{cost.min}+' is planned with a worst case of exactly {cost.max}"


# ---------------------------------------------------------------------------------------------------
# AUD-19 (P2): an unknown city after the clarification round is reported as a temporary lookup failure
# ---------------------------------------------------------------------------------------------------

async def test_aud19_unknown_city_message_is_not_an_outage() -> None:
    deps = build_deps(fixture_settings())
    result = await run_baseline(PlanRequest(city="Atlantisville", budget=1000), deps, asked_before=True)
    assert result.status == "no_feasible_plan"
    assert "right now" not in (result.message or "") and "try again" not in (result.message or "").lower(), result.message


# ---------------------------------------------------------------------------------------------------
# AUD-20 (P2): priceRange nanos are dropped, so a listed £14.99 maximum becomes £14
# ---------------------------------------------------------------------------------------------------

def test_aud20_fractional_upper_price_rounds_up() -> None:
    place = {"priceRange": {"startPrice": {"currencyCode": "GBP", "units": "10", "nanos": 500000000},
                            "endPrice": {"currencyCode": "GBP", "units": "14", "nanos": 990000000}}}
    assert _cost(place, "restaurant", "GBP").max >= 15
