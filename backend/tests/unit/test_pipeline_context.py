from datetime import UTC, datetime

import pytest

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
    assert ctx.end_min == ctx.start_min  # the original 15:00-18:00 window has passed; no extra hours invented
    assert any("usual start time has passed" in a for a in prefs.assumptions)


def test_unknown_fault_names_are_logged_and_known_ones_are_not(caplog: pytest.LogCaptureFixture) -> None:
    from app.providers.faults import KNOWN_FAULTS

    caplog.set_level("WARNING", logger="app.deps")
    build_deps(fixture_settings(fault="places_http_5O3"))  # a typo
    assert [r.getMessage() for r in caplog.records] == ["unknown FAULT 'places_http_5O3' ignored"]
    caplog.clear()
    for fault in sorted(KNOWN_FAULTS):
        build_deps(fixture_settings(fault=fault))
    build_deps(fixture_settings(app_env="prod", fault="anything"))  # prod ignores FAULT entirely
    assert caplog.records == []


def test_fault_names_match_the_fake_model_and_the_eval_spec() -> None:
    import json
    from pathlib import Path

    import app.llm.fake as fake
    from app.providers.faults import KNOWN_FAULTS, LLM_FAULTS

    source = Path(fake.__file__).read_text(encoding="utf-8")
    assert all(f'"{name}"' in source for name in LLM_FAULTS)
    spec = json.loads((Path(__file__).resolve().parents[3] / "eval_cases.json").read_text(encoding="utf-8"))
    harness_only = {"exceed_per_ip_limit"}  # evals/cases.py HARNESS_FAULTS
    used = {c["fault"] for c in spec["cases"] if c.get("fault")}
    assert used <= KNOWN_FAULTS | harness_only, sorted(used - KNOWN_FAULTS - harness_only)


async def test_events_fault_only_wraps_enabled_events() -> None:
    from app.providers.base import ProviderError
    from app.providers.faults import FailingEvents, FailingPlaces
    from tests.providers.conftest import make_ctx

    disabled = build_deps(fixture_settings(fault="events_search_error", events_provider="none"))
    assert disabled.events is None  # events off: nothing to fail, no "events unavailable" degradation
    assert await FailingEvents(None).search(["music"], make_ctx()) == []
    enabled = build_deps(fixture_settings(fault="events_search_error"))
    assert isinstance(enabled.events, FailingEvents)
    with pytest.raises(ProviderError):
        await enabled.events.search(["music"], make_ctx())
    places = build_deps(fixture_settings(fault="places_http_503")).places
    assert isinstance(places, FailingPlaces) and places.status == "unavailable"
