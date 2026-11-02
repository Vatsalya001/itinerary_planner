"""Deterministic pipeline steps shared by the no-LLM baseline and the LangGraph nodes."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

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
from app.domain.sun import SAFETY_MARGIN_MIN, sun_times
from app.domain.timeutil import fmt_clock, next_saturday
from app.providers.base import PlacesProvider, ProviderError
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
    """Target date, time window, weather (with an astronomical sunset fallback) and pacing.

    If the plan is for today (a Saturday) and the start time has already passed, the start moves to the
    next quarter hour (the window length is kept) and an assumption is added to ``prefs``.
    """
    degradations: list[str] = []
    day = prefs.date or next_saturday(now, location.timezone)
    local_now = now.astimezone(ZoneInfo(location.timezone))
    if day == local_now.date():
        earliest = local_now.hour * 60 + local_now.minute + 15
        earliest = -(-earliest // 15) * 15  # round up to the quarter hour
        if prefs.start_min < earliest:
            prefs.start_min = earliest
            prefs.assumptions = [a for a in prefs.assumptions if not a.startswith("Starting at ")]
            prefs.assumptions.append(f"Starting at {fmt_clock(earliest)} because the usual start time has passed")

    tracer.emit("tool_called", node="get_weather", tool="get_weather", reason=f"forecast for {day.isoformat()}")
    t0 = time.perf_counter()
    try:
        weather = await deps.weather.forecast(location, day)
    except Exception:  # weather is optional; never fail the run on it
        weather = Weather(status="unavailable", summary="Weather not available")
    elapsed = int((time.perf_counter() - t0) * 1000)
    if weather.status == "ok":
        tracer.emit("tool_completed", node="get_weather", tool="get_weather", duration_ms=elapsed,
                    summary=weather.summary or "forecast received")
    else:
        degradations.append(WEATHER_UNAVAILABLE)
        tracer.emit("tool_failed", node="get_weather", tool="get_weather", duration_ms=elapsed,
                    summary="Weather unavailable; planning without a forecast",
                    data={"code": "weather_unavailable", "recovered": True})
    if weather.sunset_min is None:
        # Computed sun times carry formula error; a safety margin keeps walks out of the dusk window.
        times = sun_times(day, location.lat, location.lng, location.timezone)
        if times is not None:
            weather = weather.model_copy(
                update={"sunrise_min": times[0] + SAFETY_MARGIN_MIN, "sunset_min": times[1] - SAFETY_MARGIN_MIN}
            )

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
    async def run(provider_name: str, q: PlaceQuery, provider: PlacesProvider) -> list[Candidate]:
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
