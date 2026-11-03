"""Independent audit (service layer): storage, checkpoint naming, share-link redaction, retention.

Every test reproduces a confirmed bug and is marked ``xfail(strict=True)``: it fails today and turns into an
XPASS (a strict failure) once the bug is fixed, so the marker must then be removed. Offline only: fixture
providers, the scripted fake model / no-LLM baseline, in-memory or temp-dir SQLite, and a fake psycopg pool that
adapts parameters with psycopg's real client-side Transformer (no Postgres server).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

import app.main as main_module
from app.checkpoint import checkpoint_path_for
from app.deps import fixture_settings
from app.domain.schemas import Candidate, Omission, PlaceQuery, PlanRequest, PlanResult, WorldContext
from app.graph.nodes.finalize import with_omission_names
from app.graph.runner import stream_plan
from app.main import create_app
from app.providers.fixture import FixturePlaces
from app.store import redact, retention
from app.store.factory import create_store
from app.store.postgres import PostgresStore
from app.store.sqlite import SQLiteStore
from tests.api.plan_factories import FakePlaces, MutableClock, google_candidate, make_result
from tests.api.plan_factories import make_deps as make_api_deps
from tests.graph.helpers import make_deps as make_graph_deps

OMITTED_GOOGLE_ID = "gp:ChIJomittedplace0003"
OMITTED_GOOGLE_NAME = "Brewhouse Omitted Taproom"


@pytest.fixture(autouse=True)
def _quiet_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})


class GoogleLikePlaces(FixturePlaces):
    """Frozen-world places relabelled as Google Places results (``gp:`` IDs, provider ``google_places``)."""

    name = "google_places"

    async def search(self, query: PlaceQuery, ctx: WorldContext) -> list[Candidate]:
        found = await super().search(query, ctx)
        return [c.model_copy(update={
            "id": "gp:" + c.id.replace(":", "_"),
            "source": c.source.model_copy(update={"provider": "google_places", "attribution": "Google Maps",
                                                  "last_checked": None}),
        }) for c in found]


# ---------------------------------------------------------------------------------------------------------
# AUD-S40: Google display names of omitted candidates are stored in share records
# ---------------------------------------------------------------------------------------------------------


def test_aud_s40_share_record_never_stores_omitted_google_names() -> None:
    result = make_result()
    assert result.plan is not None
    omitted_place = google_candidate(OMITTED_GOOGLE_ID, OMITTED_GOOGLE_NAME)
    # What finalize does for every plan: names are filled from the ledger for omitted candidate IDs.
    plan = with_omission_names(
        result.plan.model_copy(update={"omitted": [Omission(ref=OMITTED_GOOGLE_ID, reason="Too lively for today")]}),
        {OMITTED_GOOGLE_ID: omitted_place})
    assert plan.omitted[0].name == OMITTED_GOOGLE_NAME  # precondition: the server put the Google name there
    record = redact.for_storage(result.model_copy(update={"plan": plan, "share_id": "share_s40"}))
    assert OMITTED_GOOGLE_NAME not in json.dumps(record, ensure_ascii=False)


@pytest.mark.parametrize("model", ["baseline:deterministic", "fake:agent"])
async def test_aud_s40_end_to_end_plan_share_record_keeps_omitted_google_names(model: str) -> None:
    deps = make_graph_deps(primary_model=model)
    deps.places = GoogleLikePlaces()
    request = PlanRequest(city="Bangalore", budget=2000, available_time="4 hours",
                          interests=["food", "music", "walks"], mood="tired")
    events = [e async for e in stream_plan(request, deps)]
    result = PlanResult.model_validate(next(e for e in events if e.type == "result").data)
    assert result.status in ("complete", "fallback") and result.plan is not None
    google_omitted = [o.name for o in result.plan.omitted if o.ref.startswith("gp:") and o.name]
    if not google_omitted:
        pytest.skip("this planner version produced no omitted Google candidates (see the direct test above)")
    raw = json.dumps(redact.for_storage(result), ensure_ascii=False)
    assert [n for n in google_omitted if n in raw] == []


# ---------------------------------------------------------------------------------------------------------
# AUD-S41: two app databases in one directory can share one checkpoint file
# ---------------------------------------------------------------------------------------------------------


def test_aud_s41_checkpoint_files_are_distinct_per_app_database() -> None:
    urls = ["sqlite:////srv/data/app.db", "sqlite:////srv/data/app.sqlite3", "sqlite:////srv/data/app"]
    paths = [checkpoint_path_for(u) for u in urls]
    assert len(set(paths)) == len(urls), paths  # module docstring: "never share checkpoints"


# ---------------------------------------------------------------------------------------------------------
# AUD-S42: a checkpoint whose cleanup failed (or whose process died mid-run) is never swept
# ---------------------------------------------------------------------------------------------------------


class FlakyDeleteSaver(InMemorySaver):
    """Checkpointer whose first adelete_thread fails like a transient DB error (later calls work)."""

    def __init__(self) -> None:
        super().__init__()
        self.delete_calls = 0

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_calls += 1
        if self.delete_calls == 1:
            raise OSError("database is temporarily unavailable")
        await super().adelete_thread(thread_id)


async def test_aud_s42_orphaned_run_checkpoint_is_eventually_swept() -> None:
    free_text = "Bangalore, 2000 rupees, 4 hours, recovering from a rough week, vegetarian"
    saver = FlakyDeleteSaver()
    deps = make_graph_deps(primary_model="baseline:deterministic")
    deps.checkpointer = saver
    events = [e async for e in stream_plan(PlanRequest(free_text=free_text), deps)]
    result = PlanResult.model_validate(next(e for e in events if e.type == "result").data)
    assert result.status in ("complete", "fallback", "no_feasible_plan")
    thread_id = events[0].data["thread_id"]  # type: ignore[index]
    config: Any = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    leftover = await saver.aget_tuple(config)
    assert leftover is not None and saver.delete_calls == 1  # the runner's cleanup failed once (logged only)
    assert free_text in json.dumps(leftover.checkpoint["channel_values"].get("request"))  # raw user text kept

    clock = MutableClock(datetime.now(UTC) + timedelta(days=30))
    store = SQLiteStore(":memory:", clock=clock)
    await store.init()
    try:
        await retention.sweep(store, saver)  # a month of hourly sweeps later (the delete would work now)
    finally:
        await store.close()
    assert await saver.aget_tuple(config) is None


# ---------------------------------------------------------------------------------------------------------
# AUD-S43: NUL characters are accepted from clients; Postgres refuses them (500 / unsaved share), SQLite stores them
# ---------------------------------------------------------------------------------------------------------


class _AdaptingCursor:
    """Cursor that adapts parameters exactly as psycopg does client-side, then answers a few store queries."""

    def __init__(self, pool: AdaptingFakePgPool) -> None:
        self.pool = pool
        self.description: Any = None
        self.rowcount = 0
        self._rows: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> _AdaptingCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        from psycopg.adapt import PyFormat, Transformer

        Transformer().dump_sequence(params, [PyFormat.AUTO] * len(params))  # raises like psycopg would
        self.pool.statements.append(sql)
        if sql.startswith("SELECT record FROM plans"):
            self.description, self._rows = ("record",), [(self.pool.record,)]
        elif "RETURNING count" in sql:  # rate-limit upserts: always the first hit
            self.description, self._rows = ("count",), [(1,)]
        elif sql.startswith("SELECT"):
            self.description, self._rows = ("x",), []
        else:
            self.description, self._rows = None, []

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, pool: AdaptingFakePgPool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _AdaptingCursor:
        return _AdaptingCursor(self.pool)

    def transaction(self) -> _Conn:
        return self


class AdaptingFakePgPool:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.statements: list[str] = []

    def connection(self) -> _Conn:
        return _Conn(self)

    async def close(self) -> None:
        return None


@asynccontextmanager
async def _client(store: Any, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = fixture_settings(frontend_dist=str(tmp_path / "no-dist"))
    deps = make_api_deps(settings, places=FakePlaces(), store=store)
    deps.checkpointer = InMemorySaver()
    app = create_app(settings, deps)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.40", 4321))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def test_aud_s43_feedback_with_nul_behaves_the_same_on_both_stores(tmp_path: Path) -> None:
    body = {"rating": "down", "reason": "the second stop was closed\u0000"}
    record = redact.for_storage(make_result().model_copy(update={"share_id": "share_s43"}))

    sqlite_store = SQLiteStore(":memory:")
    await sqlite_store.init()
    try:
        await sqlite_store.save_plan("share_s43", record)
        async with _client(sqlite_store, tmp_path) as client:
            sqlite_status = (await client.post("/api/plans/share_s43/feedback", json=body)).status_code
    finally:
        await sqlite_store.close()

    pg_store = PostgresStore("postgresql://offline/audit")
    pool = AdaptingFakePgPool(record)
    pg_store._pool = pool
    async with _client(pg_store, tmp_path) as client:
        pg_status = (await client.post("/api/plans/share_s43/feedback", json=body)).status_code
    pg_store._pool = None

    assert sqlite_status == 400  # rejected before either store sees invalid text
    assert pg_status == sqlite_status, f"postgres answered {pg_status}"


# ---------------------------------------------------------------------------------------------------------
# AUD-S44: "~" in a SQLite DATABASE_URL: mkdir and the checkpointer expand it, the store connection does not
# ---------------------------------------------------------------------------------------------------------


async def test_aud_s44_tilde_sqlite_url_opens_the_expanded_path(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)  # a literal "./~" directory would land here, never in the repo
    url = "sqlite:///~/psp/app.db"
    assert checkpoint_path_for(url) == str(home / "psp" / "app.checkpoints.db")  # checkpointer: expanded
    store = create_store(url)
    try:
        await store.init()  # raises sqlite3.OperationalError: unable to open database file
        assert (home / "psp" / "app.db").is_file()
    finally:
        await store.close()
