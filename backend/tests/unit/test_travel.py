import math

import pytest
import yaml

from app.domain.travel import (
    CITIES_PATH,
    DETOUR_FACTOR,
    HeuristicTravel,
    TransferUnknownError,
    coords_valid,
    haversine_km,
)
from app.providers.base import TravelEstimator
from tests.factories import LONDON, MUMBAI, make_candidate, make_ctx

travel = HeuristicTravel()


def test_haversine_known_distances():
    assert haversine_km(12.9716, 77.5946, 12.9716, 77.5946) == 0.0
    # Bangalore -> Mumbai is roughly 840 km great-circle
    assert 820 < haversine_km(12.9716, 77.5946, 19.0760, 72.8777) < 860
    # one degree of latitude is about 111 km
    assert haversine_km(0.0, 10.0, 1.0, 10.0) == pytest.approx(111.2, abs=0.3)


def test_satisfies_protocol():
    assert isinstance(travel, TravelEstimator)


def test_walk_leg_is_free_and_uses_walking_speed():
    a = make_candidate("fx:a", "cafe", offset_km=(0.0, 0.0))
    b = make_candidate("fx:b", "bookstore", offset_km=(0.5, 0.0))
    leg = travel.leg(a, b, make_ctx())
    assert leg.mode == "walk"
    assert leg.distance_km <= 1.2
    assert leg.distance_km == pytest.approx(haversine_km(a.lat, a.lng, b.lat, b.lng) * DETOUR_FACTOR, abs=0.01)
    assert leg.minutes == math.ceil(leg.distance_km / 4.5 * 60)
    assert leg.minutes_max == math.ceil(leg.minutes * 1.3)
    assert leg.fare.min == leg.fare.max == 0
    assert leg.fare.basis == "free"
    assert leg.basis == "estimate"


def test_bangalore_auto_leg_fare_and_minutes():
    a = make_candidate("fx:a", "cafe", offset_km=(0.0, 0.0))
    b = make_candidate("fx:b", "museum", offset_km=(4.0, 0.0))
    ctx = make_ctx()
    leg = travel.leg(a, b, ctx)
    cfg = yaml.safe_load(CITIES_PATH.read_text(encoding="utf-8"))["bangalore"]
    fare = cfg["fares"]["auto"]
    assert leg.mode == "auto"
    expected = fare["base"] + max(0.0, leg.distance_km - fare["included_km"]) * fare["per_km"]
    assert leg.fare.min == round(expected)
    assert leg.fare.max == round(expected * 1.25)
    assert leg.fare.basis == "category_estimate"
    assert leg.fare.currency == "INR"
    expected_minutes = math.ceil(cfg["pickup_min"]["auto"] + leg.distance_km / cfg["speeds_kmh"]["auto"] * 60)
    assert leg.minutes == expected_minutes
    assert leg.minutes_max == math.ceil(expected_minutes * 1.3)


def test_walk_threshold_boundary():
    a = make_candidate("fx:a", "cafe")
    # just above the 1.2 km walking threshold after the detour factor
    b = make_candidate("fx:b", "cafe", offset_km=(1.25 / DETOUR_FACTOR, 0.0))
    assert travel.leg(a, b, make_ctx()).mode == "auto"


def test_city_aliases_and_defaults():
    ctx = make_ctx()
    assert travel.city_key(ctx) == "bangalore"
    alias = ctx.model_copy(update={"location": ctx.location.model_copy(update={"name": "Bengaluru"})})
    assert travel.city_key(alias) == "bangalore"
    assert travel.city_key(make_ctx(location=MUMBAI)) == "mumbai"
    bombay = MUMBAI.model_copy(update={"name": "Bombay"})
    assert travel.city_key(make_ctx(location=bombay)) == "mumbai"
    jaipur = MUMBAI.model_copy(update={"name": "Jaipur", "id": "fixture:jaipur"})
    assert travel.city_key(make_ctx(location=jaipur)) == "default_IN"
    assert travel.city_key(make_ctx(location=LONDON)) == "default"


def test_non_indian_city_uses_cab_in_local_currency():
    ctx = make_ctx(location=LONDON)
    a = make_candidate("fx:a", "cafe", lat=51.5074, lng=-0.1278)
    b = make_candidate("fx:b", "museum", lat=51.5300, lng=-0.1278)
    leg = travel.leg(a, b, ctx)
    assert leg.mode == "cab"
    assert leg.fare.currency == "GBP"
    assert leg.fare.max >= leg.fare.min > 0


def test_deterministic():
    a = make_candidate("fx:a", "cafe")
    b = make_candidate("fx:b", "park", offset_km=(3.0, 2.0))
    ctx = make_ctx()
    assert travel.leg(a, b, ctx) == travel.leg(a, b, ctx)
    assert HeuristicTravel().leg(a, b, ctx) == travel.leg(a, b, ctx)


def test_missing_coordinates_raise():
    a = make_candidate("fx:a", "cafe")
    b = make_candidate("fx:b", "cafe", lat=float("nan"))
    with pytest.raises(TransferUnknownError):
        travel.leg(a, b, make_ctx())
    assert not coords_valid(float("nan"), 77.0)
    assert not coords_valid(0.0, 0.0)
    assert not coords_valid(95.0, 10.0)
    assert coords_valid(12.97, 77.59)


def test_every_fare_entry_has_source_and_date():
    raw = yaml.safe_load(CITIES_PATH.read_text(encoding="utf-8"))
    assert {"bangalore", "mumbai", "delhi", "default_IN", "default"} <= set(raw)
    for key, entry in raw.items():
        assert entry["speeds_kmh"]["walk"] == 4.5, key
        fares = entry.get("fares") or entry.get("fares_by_currency")
        assert fares, key
        for name, fare in fares.items():
            assert fare["source"], (key, name)
            assert fare["as_of"], (key, name)
            assert "verified" not in fare["source"].lower() or "unverified" in fare["source"].lower() \
                or "not checked" in fare["source"].lower(), (key, name)
    assert "bengaluru" in raw["bangalore"]["aliases"]
    assert "bombay" in raw["mumbai"]["aliases"]
    assert "new delhi" in raw["delhi"]["aliases"]


def test_same_name_in_another_country_does_not_get_indian_fares():
    delhi_ca = LONDON.model_copy(update={"id": "x:delhi-ca", "name": "Delhi", "country_code": "CA", "currency": "CAD"})
    ctx = make_ctx(location=delhi_ca)
    assert travel.city_key(ctx) == "default"
    a = make_candidate("fx:a", "cafe")
    leg = travel.leg(a, make_candidate("fx:b", "cafe", offset_km=(3.0, 0.0)), ctx)
    assert (leg.mode, leg.fare.currency) == ("cab", "CAD")


def test_never_prices_legs_in_a_fallback_currency():
    # a currency with no fare table: walking legs are free in that currency, motorised legs are not priced
    krona = LONDON.model_copy(update={"id": "x:rkv", "name": "Reykjavik", "country_code": "IS", "currency": "ISK"})
    ctx = make_ctx(location=krona)
    a = make_candidate("fx:a", "cafe")
    walk = travel.leg(a, make_candidate("fx:b", "cafe", offset_km=(0.3, 0.0)), ctx)
    assert walk.mode == "walk" and walk.fare.currency == "ISK"
    with pytest.raises(TransferUnknownError):
        travel.leg(a, make_candidate("fx:c", "cafe", offset_km=(3.0, 0.0)), ctx)
    # no currency on the location: the country's currency from countries.yaml
    paris = LONDON.model_copy(update={"id": "x:paris", "name": "Paris", "country_code": "FR", "currency": None})
    far = travel.leg(a, make_candidate("fx:d", "cafe", offset_km=(3.0, 0.0)), make_ctx(location=paris))
    assert far.fare.currency == "EUR"
