"""Dependency container: builds providers from settings. Tests and evals build their own with fixtures."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings
from app.providers.base import EventsProvider, Geocoder, PlacesProvider, TravelEstimator, WeatherProvider


@dataclass
class Deps:
    settings: Settings
    places: PlacesProvider
    snapshot: PlacesProvider | None  # used when the live places provider fails or is over quota
    events: EventsProvider | None
    geocoder: Geocoder
    weather: WeatherProvider
    travel: TravelEstimator
    clock: Callable[[], datetime]
    http: httpx.AsyncClient | None = None
    checkpointer: Any = None  # LangGraph checkpointer (InMemorySaver / AsyncSqliteSaver / AsyncPostgresSaver)
    store: Any = None  # app.store.base.Store
    extras: dict[str, Any] = field(default_factory=dict)

    def now(self) -> datetime:
        return self.clock()


def make_clock(settings: Settings) -> Callable[[], datetime]:
    fixed = settings.fixed_now
    if fixed is not None and not settings.is_prod:
        frozen = fixed if fixed.tzinfo else fixed.replace(tzinfo=UTC)
        return lambda: frozen
    return lambda: datetime.now(UTC)


def build_deps(settings: Settings, http: httpx.AsyncClient | None = None) -> Deps:
    """Wire providers for the configured mode. Imports are local so fixture-only runs need no network."""
    from app.domain.travel import HeuristicTravel
    from app.providers.fixture import FixtureEvents, FixtureGeocoder, FixturePlaces, FixtureWeather
    from app.providers.snapshot import SnapshotPlaces

    client = http
    needs_http = (
        settings.resolved_places_provider() == "google"
        or settings.resolved_events_provider() == "tavily"
        or settings.weather_provider == "open_meteo"
        or settings.geocoder == "open_meteo"
    )
    if client is None and needs_http:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=4.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "SaturdayPlanner/0.1"},
        )

    snapshot: PlacesProvider = SnapshotPlaces()
    places: PlacesProvider
    match settings.resolved_places_provider():
        case "google":
            from app.providers.google_places import GooglePlaces

            assert settings.google_maps_api_key is not None and client is not None
            places = GooglePlaces(settings.google_maps_api_key.get_secret_value(), client)
        case "fixture":
            places = FixturePlaces()
        case _:
            places = snapshot

    events: EventsProvider | None
    match settings.resolved_events_provider():
        case "tavily":
            from app.providers.events_web import TavilyEvents

            assert settings.tavily_api_key is not None and client is not None
            events = TavilyEvents(settings.tavily_api_key.get_secret_value(), client)
        case "fixture":
            events = FixtureEvents()
        case _:
            events = None

    geocoder: Geocoder
    weather: WeatherProvider
    if settings.geocoder == "fixture":
        geocoder = FixtureGeocoder()
    else:
        from app.providers.open_meteo import OpenMeteoGeocoder

        assert client is not None
        geocoder = OpenMeteoGeocoder(client)
    if settings.weather_provider == "fixture":
        weather = FixtureWeather(settings.fixture_weather)
    else:
        from app.providers.open_meteo import OpenMeteoWeather

        assert client is not None
        weather = OpenMeteoWeather(client)

    fault = settings.active_fault()
    if fault == "places_http_503":
        from app.providers.faults import FailingPlaces

        places = FailingPlaces(places)
    elif fault == "events_search_error":
        from app.providers.faults import FailingEvents

        events = FailingEvents(events)
    elif fault == "geocoder_down":
        from app.providers.faults import FailingGeocoder

        geocoder = FailingGeocoder()

    return Deps(
        settings=settings,
        places=places,
        snapshot=snapshot if getattr(places, "inner", places) is not snapshot else None,
        events=events,
        geocoder=geocoder,
        weather=weather,
        travel=HeuristicTravel(),
        clock=make_clock(settings),
        http=client,
    )


def fixture_settings(**overrides: Any) -> Settings:
    """Settings for the frozen world (tests, offline evals, model evals)."""
    base: dict[str, Any] = {
        "app_env": "test",
        "places_provider": "fixture",
        "events_provider": "fixture",
        "weather_provider": "fixture",
        "geocoder": "fixture",
        "fixed_now": datetime(2026, 11, 5, 4, 30, tzinfo=UTC),  # 10:00 IST Thursday
        "database_url": "sqlite:///:memory:",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)
