"""Test-only provider fault injection (settings.fault; ignored in prod via Settings.active_fault()).

``build_deps`` applies the provider faults below; model faults (``LLM_FAULTS``) are read by the scripted fake
model (app/llm/fake.py). Any other ``FAULT`` value is logged as a warning and ignored. Unavailable weather is not
a fault: use ``FIXTURE_WEATHER=unavailable``.
"""

from __future__ import annotations

from app.domain.schemas import Candidate, Location, PlaceQuery, WorldContext
from app.providers.base import EventsProvider, PlacesProvider, ProviderError

PROVIDER_FAULTS = frozenset({"places_http_503", "events_search_error", "geocoder_down"})
LLM_FAULTS = frozenset({
    "llm_timeout", "llm_refusal", "primary_provider_503", "submit_plan_unknown_ids_twice", "text_only_final_turn",
    "parser_omits_diet_and_agent_selects_nonveg",
})
KNOWN_FAULTS = PROVIDER_FAULTS | LLM_FAULTS


class FailingPlaces:
    """Live places provider that always fails like an HTTP 503 (exercises the snapshot fallback)."""

    status = "unavailable"  # what /api/health reports for a live provider that keeps failing

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
    """Events provider whose search fails. ``build_deps`` only wraps an enabled provider; wrapping ``None``
    (events disabled) finds nothing instead of failing, so disabled events never look like an outage."""

    def __init__(self, inner: EventsProvider | None) -> None:
        self.inner = inner
        self.name = inner.name if inner is not None else "events"

    async def search(self, interests: list[str], ctx: WorldContext, max_price: int | None = None) -> list[Candidate]:
        if self.inner is None:
            return []
        raise ProviderError(self.name, "search_error", "injected fault", retryable=True)


class FailingGeocoder:
    async def resolve(self, city: str) -> list[Location]:
        raise ProviderError("geocoder", "http_503", "injected fault", retryable=True)
