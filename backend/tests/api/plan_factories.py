"""Schema-valid plan objects, a fake runner and a fake places provider for API and store tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from typing import Any

from app.config import Settings
from app.deps import Deps, make_clock
from app.domain.schemas import (
    Candidate,
    CanonicalPlan,
    CheckItem,
    Constraint,
    CostRange,
    Energy,
    Evidence,
    Hours,
    Leg,
    PlanRequest,
    PlanResult,
    Preferences,
    ScheduledStop,
    Source,
    Totals,
    TraceEvent,
    Weather,
    WorldContext,
)
from app.providers.base import ProviderError

GOOGLE_ID = "gp:ChIJquietleaf0001"
GOOGLE_PLAN_B_ID = "gp:ChIJbookcafe0002"
SNAP_ID = "snap:cubbon_park"
GOOGLE_NAME = "Quiet Leaf Cafe"
GOOGLE_PLAN_B_NAME = "Rainy Day Book Cafe"
GOOGLE_ADDRESS = "12 Example Road, Ashok Nagar"
MOOD = "tired but wants to do something fun"
CONSTRAINT_TEXT = "vegetarian please, nothing crowded"
TARGET_DATE = date(2026, 11, 7)
NOW = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)


def google_candidate(cid: str = GOOGLE_ID, name: str = GOOGLE_NAME, rating: float = 4.5) -> Candidate:
    return Candidate(
        id=cid, name=name, category="cafe", city_id="fixture:bangalore", lat=12.9716, lng=77.5946,
        address=GOOGLE_ADDRESS, neighbourhood="Ashok Nagar",
        cost=CostRange(min=200, max=400, currency="INR", basis="price_level_estimate"),
        rating=rating, rating_count=1200, hours=Hours(status="known", periods=[(480, 1320)]),
        diet={"vegetarian": Evidence.CONFIRMED}, wheelchair=Evidence.UNKNOWN, indoor=True, seated=True,
        effort="low", popularity="medium", tags=["coffee", "food"], duration_range=(30, 60, 90),
        maps_url=f"https://maps.google.com/?cid={cid[3:]}",
        source=Source(provider="google_places", url=f"https://maps.google.com/?cid={cid[3:]}",
                      retrieved_at=NOW, attribution="Google Maps"),
    )


def snapshot_candidate() -> Candidate:
    return Candidate(
        id=SNAP_ID, name="Cubbon Park", category="park", city_id="fixture:bangalore", lat=12.9763, lng=77.5929,
        address="Kasturba Road", cost=CostRange(min=0, max=0, currency="INR", basis="free"),
        hours=Hours(status="known", periods=[(360, 1080)]), indoor=False, seated=False, effort="low",
        tags=["walks"], duration_range=(30, 45, 90),
        maps_url="https://www.google.com/maps/search/?api=1&query=12.9763,77.5929",
        source=Source(provider="curated_snapshot", last_checked=date(2026, 10, 30),
                      attribution="© OpenStreetMap contributors"),
    )


def make_result(run_id: str = "r_test000001", status: str = "complete", mood: str = MOOD,
                thread_id: str | None = None) -> PlanResult:
    cafe, park, plan_b = google_candidate(), snapshot_candidate(), google_candidate(GOOGLE_PLAN_B_ID,
                                                                                  GOOGLE_PLAN_B_NAME, 4.2)
    weather = Weather(status="ok", summary="Clear, 27°C", precip_prob_max=10, temp_max_c=27.0, temp_min_c=19.0,
                      weather_code=0, sunrise_min=370, sunset_min=1070)
    leg = Leg(from_id=cafe.id, to_id=park.id, mode="walk", distance_km=0.6, minutes=9, minutes_max=12,
              fare=CostRange(min=0, max=0, currency="INR", basis="free"))
    stops = [
        ScheduledStop(candidate=cafe, start_min=900, end_min=960, duration_min=60,
                      why=f"{GOOGLE_NAME} is a calm, seated start with confirmed vegetarian options",
                      tradeoff="Slightly away from the park", plan_b=plan_b),
        ScheduledStop(candidate=park, start_min=982, end_min=1027, duration_min=45,
                      why="An easy shaded walk to finish", warnings=[]),
    ]
    totals = Totals(currency="INR", stops_cost_min=200, stops_cost_max=400, legs_cost_min=0, legs_cost_max=0,
                    contingency=40, worst_case=440, budget=2000, budget_left=1560, elapsed_min=127,
                    available_minutes=240, minutes_left=113, walking_min=54)
    plan = CanonicalPlan(date=TARGET_DATE, city="Bangalore", timezone="Asia/Kolkata", start_min=900, end_min=1027,
                         stops=stops, legs=[leg], totals=totals, summary_note="A gentle afternoon", weather=weather,
                         maps_route_url="https://www.google.com/maps/dir/?api=1&origin=12.971600,77.594600")
    prefs = Preferences(
        city="Bangalore", budget=2000, currency="INR", start_min=900, available_minutes=240, date=TARGET_DATE,
        mood=mood, energy=Energy.LOW, interests=["food", "walks"],
        hard_constraints=[Constraint(kind="diet", value="vegetarian", source_text=CONSTRAINT_TEXT)],
        soft_preferences=["somewhere quiet"], assumptions=["Start time defaulted to 15:00"],
    )
    if status in ("complete", "fallback"):
        return PlanResult(
            run_id=run_id, status=status, mode="agent" if status == "complete" else "deterministic",  # type: ignore[arg-type]
            model="scripted:fake", plan=plan, preferences=prefs,
            checks=[CheckItem(name="diet", status="pass", detail=f"{GOOGLE_NAME} confirms vegetarian food")],
            degradations=["events_unavailable"] if status == "fallback" else [], latency_ms=None,
        )
    return PlanResult(run_id=run_id, status=status, preferences=prefs, thread_id=thread_id,  # type: ignore[arg-type]
                      message="Which city?" if status == "needs_clarification" else None)


FINALIZE_DATA = {
    "usage": {"input_tokens": 1234, "output_tokens": 321, "cache_read_tokens": 0, "cost_usd": 0.0123,
              "llm_calls": 3},
    "submit_attempts": 2,
    "tool_calls": {"search_places": 2, "check_plan": 1},
    "model": "scripted:fake",
}


def make_events(run_id: str, result: PlanResult) -> list[TraceEvent]:
    def ev(seq: int, type_: str, **kw: Any) -> TraceEvent:
        return TraceEvent(type=type_, run_id=run_id, seq=seq, ts=datetime.now(UTC), **kw)  # type: ignore[arg-type]

    return [
        ev(1, "run_started"),
        ev(2, "node_started", node="parse_preferences"),
        ev(3, "node_completed", node="parse_preferences", duration_ms=12, summary="City, budget and 2 constraints"),
        ev(4, "tool_called", node="tools", tool="search_places", reason="Quiet vegetarian cafe for a seated start"),
        ev(5, "tool_completed", node="tools", tool="search_places", duration_ms=40, summary="8 places"),
        ev(6, "validation", node="validate_plan", summary="Valid", data={"violations": []}),
        ev(7, "node_started", node="finalize"),
        ev(8, "node_completed", node="finalize", duration_ms=3, data=dict(FINALIZE_DATA)),
        ev(9, "result", data=result.model_dump(mode="json")),
    ]


class FakeRunner:
    """Stands in for app.graph.runner.stream_plan / run_plan."""

    def __init__(self, result_factory: Callable[..., PlanResult] | None = None, delay: float = 0.0,
                 delay_before_result: float = 0.0, fail_after: int | None = None, echo_mood: bool = False) -> None:
        self.result_factory = result_factory or make_result
        self.delay = delay
        self.delay_before_result = delay_before_result
        self.fail_after = fail_after
        self.echo_mood = echo_mood
        self.calls: list[dict[str, Any]] = []
        self.run_plan_calls = 0
        self.completed = asyncio.Event()

    async def stream_plan(self, request: PlanRequest | None, deps: Deps, *, thread_id: str | None = None,
                          resume_answers: dict[str, str] | None = None, model_id: str | None = None,
                          run_id: str | None = None) -> AsyncIterator[TraceEvent]:
        self.calls.append({"request": request, "thread_id": thread_id, "resume_answers": resume_answers,
                           "run_id": run_id})
        rid = run_id or "r_test000001"
        kwargs: dict[str, Any] = {"run_id": rid}
        if self.echo_mood and request is not None and request.mood:
            kwargs["mood"] = request.mood
        events = make_events(rid, self.result_factory(**kwargs))
        for i, event in enumerate(events):
            if self.fail_after is not None and i >= self.fail_after:
                raise RuntimeError("boom: secret internal detail")
            if event.type == "result" and self.delay_before_result:
                await asyncio.sleep(self.delay_before_result)
            elif self.delay:
                await asyncio.sleep(self.delay)
            yield event
        self.completed.set()

    async def run_plan(self, request: PlanRequest | None, deps: Deps, **kwargs: Any
                       ) -> tuple[PlanResult, list[TraceEvent]]:
        self.run_plan_calls += 1
        events = [e async for e in self.stream_plan(request, deps, **kwargs)]
        return PlanResult.model_validate(events[-1].data), events


class FakePlaces:
    """Places provider whose ``get`` returns fresh display data (or fails)."""

    name = "google_places"

    def __init__(self, fail: bool = False, missing: bool = False) -> None:
        self.fail = fail
        self.missing = missing
        self.get_calls: list[tuple[str, WorldContext]] = []

    async def search(self, query: Any, ctx: WorldContext) -> list[Candidate]:
        return []

    async def get(self, candidate_id: str, ctx: WorldContext) -> Candidate | None:
        self.get_calls.append((candidate_id, ctx))
        if self.fail:
            raise ProviderError("google_places", "UNAVAILABLE", "503", retryable=True)
        if self.missing:
            return None
        names = {GOOGLE_ID: GOOGLE_NAME, GOOGLE_PLAN_B_ID: GOOGLE_PLAN_B_NAME}
        if candidate_id not in names:
            return None
        fresh = google_candidate(candidate_id, names[candidate_id], rating=4.6)
        return fresh.model_copy(update={"address": "Fresh address from Places"})

    def covers(self, location: Any) -> bool:
        return True


def make_deps(settings: Settings, places: Any = None, store: Any = None) -> Deps:
    return Deps(settings=settings, places=places or FakePlaces(), snapshot=None, events=None,
                geocoder=object(), weather=object(), travel=object(),  # type: ignore[arg-type]
                clock=make_clock(settings), store=store)


def parse_sse(body: str) -> list[dict[str, Any]]:
    """TraceEvents from an SSE body; ``: keep-alive`` comment frames are skipped (see ``keepalives``)."""
    out = []
    for frame in (f for f in body.split("\n\n") if f.strip()):
        if frame.startswith(":"):
            assert frame == ": keep-alive", frame
            continue
        assert frame.startswith("data: "), frame
        assert "\n" not in frame
        out.append(json.loads(frame[len("data: "):]))
    return out


def keepalives(body: str) -> int:
    return sum(1 for f in body.split("\n\n") if f == ": keep-alive")


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now
