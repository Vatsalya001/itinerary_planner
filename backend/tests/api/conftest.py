from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

import app.main as main_module
from app.api import sse
from app.deps import Deps, fixture_settings
from app.graph import runner as runner_module
from app.main import create_app
from app.store.sqlite import SQLiteStore
from tests.api.plan_factories import FakePlaces, FakeRunner, MutableClock, make_deps

CLIENT_IP = "203.0.113.9"


@pytest.fixture(autouse=True)
def _quiet_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lifespan observability setup touches the root logger and LangSmith env; keep tests hermetic.
    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    fake = FakeRunner()
    monkeypatch.setattr(runner_module, "stream_plan", fake.stream_plan)
    monkeypatch.setattr(runner_module, "run_plan", fake.run_plan)
    return fake


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sse, "HEARTBEAT_INTERVAL_S", 0.02)


@pytest.fixture
def clock() -> MutableClock:
    # 12:05 UTC: five minutes into a 10-minute window and far from midnight, so windows never roll mid-test.
    return MutableClock(datetime(2026, 11, 5, 12, 5, tzinfo=UTC))


@pytest.fixture
async def clocked_store(clock: MutableClock) -> AsyncIterator[SQLiteStore]:
    store = SQLiteStore(":memory:", clock=clock)
    await store.init()
    yield store
    await store.close()


@pytest.fixture
def make_client(tmp_path: Any) -> Callable[..., Any]:
    """``async with make_client(**settings_overrides) as (client, app, deps):`` runs the app lifespan."""

    @asynccontextmanager
    async def _make(places: Any = None, store: Any = None, client_ip: str = CLIENT_IP,
                    **overrides: Any) -> AsyncIterator[tuple[httpx.AsyncClient, Any, Deps]]:
        overrides.setdefault("frontend_dist", str(tmp_path / "no-dist"))
        settings = fixture_settings(**overrides)
        deps = make_deps(settings, places=places or FakePlaces(), store=store)
        app = create_app(settings, deps)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=(client_ip, 4321))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                yield client, app, deps

    return _make
