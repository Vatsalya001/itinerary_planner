"""Test-only provider fault injection (settings.fault; ignored in prod via Settings.active_fault())."""

from __future__ import annotations

from datetime import date

from app.domain.schemas import Candidate, Location, PlaceQuery, Weather, WorldContext
from app.providers.base import EventsProvider, PlacesProvider, ProviderError

PROVIDER_FAULTS = {"places_http_503", "events_search_error", "geocoder_down"}


class FailingPlaces:
    """Live places provider that always fails like an HTTP 503 (exercises the snapshot fallback)."""

    def __init__(self, inner: PlacesProvider) -> None:
        self.inner = inner
        self.name = inner.name

    async def search(self, query: PlaceQuery, ctx: WorldContext) -> list[Candidate]:
        raise ProviderError(self.name, "http_503", "injected fault", retryable=True)

    async def get(self, candidate_id: str, ctx: WorldContext) -> Candidate | None:
        raise ProviderError(self.name, "http_503", "injected fault", retryable=True)

    def covers(self, location: Location) -> bool:
        return self.inner.covers(location)


class FailingEvents:
    def __init__(self, inner: EventsProvider | None) -> None:
        self.name = inner.name if inner is not None else "events"

    async def search(self, interests: list[str], ctx: WorldContext, max_price: int | None = None) -> list[Candidate]:
        raise ProviderError(self.name, "search_error", "injected fault", retryable=True)


class FailingGeocoder:
    async def resolve(self, city: str) -> list[Location]:
        raise ProviderError("geocoder", "http_503", "injected fault", retryable=True)


class UnavailableWeather:
    async def forecast(self, location: Location, day: date) -> Weather:
        return Weather(status="unavailable", summary="Weather not available")
