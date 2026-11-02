"""Provider interfaces. Live, snapshot and fixture implementations all satisfy these protocols."""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from app.domain.schemas import Candidate, Leg, Location, PlaceQuery, Weather, WorldContext


class ProviderError(Exception):
    """A provider failed. ``retryable`` is True for timeouts, 429 and 5xx."""

    def __init__(self, provider: str, code: str, message: str = "", retryable: bool = False) -> None:
        super().__init__(f"{provider}:{code} {message}".strip())
        self.provider = provider
        self.code = code
        self.retryable = retryable


@runtime_checkable
class PlacesProvider(Protocol):
    name: str  # "google_places" | "curated_snapshot" | "fixture"

    async def search(self, query: PlaceQuery, ctx: WorldContext) -> list[Candidate]:
        """Up to ``query.limit`` candidates in ``ctx.location``, best first. Raises ProviderError."""
        ...

    async def get(self, candidate_id: str, ctx: WorldContext) -> Candidate | None:
        """Re-fetch one candidate by ID (share links re-hydrate display fields this way)."""
        ...

    def covers(self, location: Location) -> bool:
        """True if this provider has data for the location (live providers return True; the snapshot
        and fixture providers return True when the location is within 40 km of a city they hold).
        Returned candidates always carry ``city_id = ctx.location.id``."""
        ...


@runtime_checkable
class EventsProvider(Protocol):
    name: str

    async def search(self, interests: list[str], ctx: WorldContext, max_price: int | None = None) -> list[Candidate]:
        """Events on ``ctx.date`` in ``ctx.location`` with a source URL each. Raises ProviderError."""
        ...


@runtime_checkable
class Geocoder(Protocol):
    async def resolve(self, city: str) -> list[Location]:
        """Matching locations, best first; [] when nothing matches. Raises ProviderError."""
        ...


@runtime_checkable
class WeatherProvider(Protocol):
    async def forecast(self, location: Location, day: date) -> Weather:
        """Daily forecast. Returns Weather(status="unavailable") rather than raising on failure."""
        ...


@runtime_checkable
class TravelEstimator(Protocol):
    def leg(self, a: Candidate, b: Candidate, ctx: WorldContext) -> Leg:
        """Deterministic leg estimate between two candidates (pure function of inputs and config)."""
        ...
