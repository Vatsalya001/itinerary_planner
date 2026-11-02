"""Deterministic pipeline steps shared by the no-LLM baseline and the LangGraph nodes."""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.deps import Deps
from app.domain.constraints import merge_constraints
from app.domain.pacing import meal_windows_between, profile_for
from app.domain.parser_rules import original_constraints
from app.domain.schemas import (
    Candidate,
    Constraint,
    Location,
    PlaceQuery,
    PlanRequest,
    Preferences,
    Weather,
    WorldContext,
)
from app.domain.timeutil import next_saturday
from app.providers.base import ProviderError
from app.trace import Tracer

PLACES_UNAVAILABLE = "Live place data is temporarily unavailable"
SNAPSHOT_USED = "Live place data unavailable; using the curated OpenStreetMap snapshot"
EVENTS_UNAVAILABLE = "Couldn't check this week's events"
WEATHER_UNAVAILABLE = "Weather not checked"


async def geocode(city: str, deps: Deps, tracer: Tracer) -> list[Location]:
    with tracer.tool("resolve_location", node="resolve_location") as info:
        matches = await deps.geocoder.resolve(city)
        info["summary"] = f"{len(matches)} match(es) for '{city}'" + (f": {matches[0].name}" if matches else "")
    return matches


async def build_context(prefs: Preferences, location: Location, deps: Deps, tracer: Tracer,
                        now: datetime) -> tuple[WorldContext, list[str]]:
    degradations: list[str] = []
    day = prefs.date or next_saturday(now, location.timezone)
    with tracer.tool("get_weather", node="get_weather") as info:
        try:
            weather = await deps.weather.forecast(location, day)
        except Exception:  # weather is optional; providers should not raise, but never fail the run on it
            weather = Weather(status="unavailable", summary="Weather not available")
        info["summary"] = weather.summary or weather.status
    if weather.status != "ok":
        degradations.append(WEATHER_UNAVAILABLE)
    start = prefs.start_min
    end = start + prefs.available_minutes
    ctx = WorldContext(
        location=location,
        date=day,
        start_min=start,
        end_min=end,
        weather=weather,
        pacing=profile_for(prefs.energy),
        meal_windows=meal_windows_between(start, end),
        now=now,
    )
    return ctx, degradations


async def search_places(queries: list[PlaceQuery], ctx: WorldContext, deps: Deps,
                        tracer: Tracer) -> tuple[list[Candidate], list[str], bool]:
    """Run queries concurrently. On live-provider failure fall back to the snapshot when it covers the city.

    Returns (candidates, degradations, places_available).
    """
    async def run(provider_name: str, q: PlaceQuery, provider) -> list[Candidate]:  # type: ignore[no-untyped-def]
        with tracer.tool("search_places", reason=q.intent, node="gather_candidates") as info:
            found = await provider.search(q, ctx)
            info["summary"] = f"{len(found)} places for '{q.intent}' ({provider_name})"
        return found

    degradations: list[str] = []
    try:
        results = await asyncio.gather(*(run(deps.places.name, q, deps.places) for q in queries))
    except ProviderError:
        if deps.snapshot is not None and deps.snapshot.covers(ctx.location):
            degradations.append(SNAPSHOT_USED)
            tracer.emit("degraded", node="gather_candidates", summary=SNAPSHOT_USED)
            results = await asyncio.gather(*(run(deps.snapshot.name, q, deps.snapshot) for q in queries))
        else:
            degradations.append(PLACES_UNAVAILABLE)
            tracer.emit("degraded", node="gather_candidates", summary=PLACES_UNAVAILABLE)
            return [], degradations, False
    seen: dict[str, Candidate] = {}
    for batch in results:
        for c in batch:
            seen.setdefault(c.id, c)
    return list(seen.values()), degradations, True


async def search_events(prefs: Preferences, ctx: WorldContext, deps: Deps,
                        tracer: Tracer) -> tuple[list[Candidate], list[str]]:
    if deps.events is None:
        return [], []
    try:
        with tracer.tool("search_events", reason="events matching interests", node="gather_candidates") as info:
            found = await deps.events.search(prefs.interests, ctx, max_price=prefs.budget)
            info["summary"] = f"{len(found)} events on {ctx.date.isoformat()}"
        return found, []
    except ProviderError:
        tracer.emit("degraded", node="gather_candidates", summary=EVENTS_UNAVAILABLE)
        return [], [EVENTS_UNAVAILABLE]


def enforced_constraints(prefs: Preferences, request: PlanRequest) -> tuple[list[Constraint], list[Constraint]]:
    """(constraints to enforce, original constraints from the raw request)."""
    original = original_constraints(request)
    return merge_constraints(prefs.hard_constraints, original), original
