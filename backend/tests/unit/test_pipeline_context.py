from datetime import UTC, datetime

from app.deps import build_deps, fixture_settings
from app.domain.schemas import Energy, Location, Preferences
from app.pipeline import WEATHER_UNAVAILABLE, build_context
from app.trace import Tracer

BLR = Location(id="fixture:bangalore", name="Bengaluru", country_code="IN", lat=12.9716, lng=77.5946,
               timezone="Asia/Kolkata", currency="INR")


async def test_unavailable_weather_still_has_sunset_and_marks_trace():
    deps = build_deps(fixture_settings(fixture_weather="unavailable"))
    tracer = Tracer("r_test")
    prefs = Preferences(city="Bangalore", energy=Energy.LOW)
    ctx, degradations = await build_context(prefs, BLR, deps, tracer, datetime(2026, 11, 5, 4, 30, tzinfo=UTC))
    assert WEATHER_UNAVAILABLE in degradations
    assert ctx.weather.status == "unavailable"
    assert ctx.weather.sunset_min is not None and abs(ctx.weather.sunset_min - (1070 - 15)) <= 6  # computed, minus safety margin
    failed = [e for e in tracer.events if e.type == "tool_failed" and e.tool == "get_weather"]
    assert failed and failed[0].data == {"code": "weather_unavailable", "recovered": True}


async def test_saturday_afternoon_start_moves_forward():
    deps = build_deps(fixture_settings())
    prefs = Preferences(city="Bangalore", start_min=900, available_minutes=180)
    now = datetime(2026, 11, 7, 12, 40, tzinfo=UTC)  # Saturday 18:10 IST
    ctx, _ = await build_context(prefs, BLR, deps, Tracer("r_test"), now)
    assert ctx.date.isoformat() == "2026-11-07"
    assert ctx.start_min == 18 * 60 + 30
    assert ctx.end_min == ctx.start_min + 180
    assert any("usual start time has passed" in a for a in prefs.assumptions)
