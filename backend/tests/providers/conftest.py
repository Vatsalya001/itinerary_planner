"""Shared fixtures for provider tests: frozen-world locations and a WorldContext factory."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest

from app.domain.pacing import profile_for
from app.domain.schemas import Energy, Location, Weather, WorldContext

SATURDAY = date(2026, 11, 7)
NOW = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)

BLR = Location(id="fixture:bangalore", name="Bengaluru", country_code="IN", admin1="Karnataka",
               lat=12.9716, lng=77.5946, timezone="Asia/Kolkata", currency="INR")
BOM = Location(id="fixture:mumbai", name="Mumbai", country_code="IN", admin1="Maharashtra",
               lat=18.9322, lng=72.8317, timezone="Asia/Kolkata", currency="INR")
JAI = Location(id="fixture:jaipur", name="Jaipur", country_code="IN", admin1="Rajasthan",
               lat=26.9124, lng=75.7873, timezone="Asia/Kolkata", currency="INR")
LON = Location(id="fixture:london", name="London", country_code="GB", admin1="England",
               lat=51.5074, lng=-0.1278, timezone="Europe/London", currency="GBP")

CtxFactory = Callable[..., WorldContext]


def make_ctx(location: Location = BLR, day: date = SATURDAY, start_min: int = 900, end_min: int = 1140,
             weather: Weather | None = None, energy: Energy = Energy.LOW) -> WorldContext:
    return WorldContext(
        location=location,
        date=day,
        start_min=start_min,
        end_min=end_min,
        weather=weather or Weather(status="unavailable", summary="Weather not available"),
        pacing=profile_for(energy),
        now=NOW,
    )


@pytest.fixture
def ctx_factory() -> CtxFactory:
    return make_ctx


@pytest.fixture
def blr_ctx() -> WorldContext:
    return make_ctx(BLR)


@pytest.fixture
def bom_ctx() -> WorldContext:
    return make_ctx(BOM)


@pytest.fixture
def locations() -> dict[str, Location]:
    return {"blr": BLR, "bom": BOM, "jai": JAI, "lon": LON}
