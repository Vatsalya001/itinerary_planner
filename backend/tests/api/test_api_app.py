"""Health, eval summary, security headers, generic 500, SPA fallback, lifespan wiring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

import app.main as main_module
from app.api import routes
from app.api.security import CSP
from app.deps import fixture_settings
from app.main import create_app
from app.store.sqlite import SQLiteStore
from tests.api.plan_factories import FakeRunner, make_deps

SENTINEL = "sk-SECRET-SENTINEL-1234567890"


def _assert_security_headers(resp: httpx.Response) -> None:
    assert resp.headers["content-security-policy"] == CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-request-id"]
    assert "access-control-allow-origin" not in resp.headers


async def test_health_reports_status_without_secrets(make_client: Any) -> None:
    async with make_client(google_maps_api_key=SENTINEL, tavily_api_key=SENTINEL, git_sha="abc1234",
                           places_provider="auto", events_provider="auto") as (client, _app, _deps):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "status": "ok", "version": "abc1234", "env": "test", "primary_model": "ollama:qwen3.5:9b",
            "places_provider": "google", "events_provider": "tavily", "db": "ok",
        }
        assert SENTINEL not in resp.text


async def test_health_degraded_when_db_fails(make_client: Any) -> None:
    async with make_client() as (client, _app, deps):
        async def broken() -> bool:
            raise RuntimeError("db down")

        deps.store.ping = broken
        body = (await client.get("/api/health")).json()
        assert body["status"] == "degraded" and body["db"] == "error"


async def test_security_headers_on_every_kind_of_response(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, _deps):
        _assert_security_headers(await client.get("/api/health"))
        _assert_security_headers(await client.get("/"))
        _assert_security_headers(await client.get("/api/plans/unknownid"))
        _assert_security_headers(await client.post("/api/plan", json={"budget": -1}))
        _assert_security_headers(await client.post("/api/plan", content=b"x" * 20_000))
        _assert_security_headers(await client.post("/api/plan", json={"city": "Pune"},
                                                   headers={"Accept": "text/event-stream"}))
        resp = await client.get("/api/health", headers={"X-Request-ID": "client_req_12345"})
        assert resp.headers["x-request-id"] == "client_req_12345"
        resp = await client.get("/api/health", headers={"X-Request-ID": "<script>"})
        assert resp.headers["x-request-id"] != "<script>"


async def test_unhandled_error_is_generic_500(make_client: Any, fake_runner: FakeRunner,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom: secret internal detail")

    async with make_client() as (client, _app, _deps):
        share_id = (await client.post("/api/plan", json={"city": "Bangalore"})).json()["share_id"]
        monkeypatch.setattr(routes.redact, "rehydrate", boom)
        resp = await client.get(f"/api/plans/{share_id}")
        assert resp.status_code == 500
        body = resp.json()
        assert body["status"] == "rejected" and body["detail"] == "Something went wrong"
        assert body["request_id"] == resp.headers["x-request-id"]
        assert "boom" not in resp.text and "Traceback" not in resp.text
        _assert_security_headers(resp)


async def test_eval_summary_from_store_then_file(make_client: Any, monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(routes, "EVALS_SUMMARY_PATH", missing)
    async with make_client() as (client, _app, deps):
        resp = await client.get("/api/evals/summary")
        assert resp.status_code == 404

        summary_file = tmp_path / "summary.json"
        summary_file.write_text(json.dumps({"source": "file", "rows": []}))
        monkeypatch.setattr(routes, "EVALS_SUMMARY_PATH", summary_file)
        assert (await client.get("/api/evals/summary")).json() == {"source": "file", "rows": []}

        await deps.store.save_eval_summary({"source": "store", "rows": [{"model": "baseline"}]})
        assert (await client.get("/api/evals/summary")).json()["source"] == "store"


async def test_spa_fallback_serves_index(make_client: Any, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Saturday</title><div id=root></div>")
    (dist / "assets" / "app-abc123.js").write_text("console.log('hi')")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("do not serve")
    async with make_client(frontend_dist=str(dist)) as (client, _app, _deps):
        for path in ("/", "/p/abc123XYZ", "/evals", "/how-it-works"):
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers["content-type"].startswith("text/html")
            assert "<title>Saturday</title>" in resp.text
            _assert_security_headers(resp)

        resp = await client.get("/assets/app-abc123.js")
        assert resp.status_code == 200 and "javascript" in resp.headers["content-type"]
        assert "immutable" in resp.headers["cache-control"]
        assert (await client.get("/favicon.svg")).text == "<svg/>"
        assert (await client.get("/assets/missing.js")).status_code == 404

        resp = await client.get("/api/does-not-exist")
        assert resp.status_code == 404 and resp.headers["content-type"].startswith("application/json")

        resp = await client.get("/..%2Fsecret.txt")
        assert "do not serve" not in resp.text
        resp = await client.get("/assets/..%2F..%2Fsecret.txt")
        assert "do not serve" not in resp.text

        assert (await client.get("/api/health")).json()["db"] == "ok"


async def test_root_hint_without_frontend(make_client: Any) -> None:
    async with make_client() as (client, _app, _deps):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Frontend not built" in resp.json()["detail"]
        assert (await client.get("/p/abc123")).status_code == 404


async def test_lifespan_creates_store_and_checkpointer_and_restores_injected_deps(tmp_path: Path,
                                                                                    monkeypatch: pytest.MonkeyPatch
                                                                                    ) -> None:
    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})
    settings = fixture_settings(database_url=f"sqlite:///{tmp_path}/db/app.db", frontend_dist=str(tmp_path / "x"))
    deps = make_deps(settings)
    app = create_app(settings, deps)
    async with app.router.lifespan_context(app):
        assert isinstance(deps.store, SQLiteStore) and deps.store.path == f"{tmp_path}/db/app.db"
        assert deps.checkpointer is not None and not isinstance(deps.checkpointer, InMemorySaver)
        assert app.state.deps is deps
        assert (tmp_path / "db" / "app.checkpoints.db").exists()
        sweeper = app.state.retention_task
        assert sweeper is not None and not sweeper.done()
    assert sweeper.cancelled() or sweeper.done()
    assert deps.store is None and deps.checkpointer is None
    assert app.state.deps is None


async def test_memory_database_uses_in_memory_checkpointer(make_client: Any) -> None:
    async with make_client() as (_client, _app, deps):
        assert isinstance(deps.checkpointer, InMemorySaver)
        assert isinstance(deps.store, SQLiteStore) and deps.store.is_memory


def test_openapi_hidden_in_prod() -> None:
    prod = create_app(fixture_settings(app_env="prod", frontend_dist="/nonexistent"), make_deps(fixture_settings()))
    assert prod.openapi_url is None
    dev = create_app(fixture_settings(frontend_dist="/nonexistent"), make_deps(fixture_settings()))
    assert dev.openapi_url == "/api/openapi.json"
    assert dev.docs_url is None


async def test_startup_sweep_deletes_abandoned_checkpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    from langgraph.checkpoint.base import empty_checkpoint

    from tests.api.plan_factories import MutableClock

    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})
    clock = MutableClock(datetime(2026, 11, 5, 12, 0, tzinfo=UTC))
    store = SQLiteStore(":memory:", clock=clock)
    await store.init()
    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "t_abandoned1", "checkpoint_ns": ""}}
    await saver.aput(cfg, empty_checkpoint(), {}, {})  # type: ignore[arg-type]
    await store.record_paused_thread("t_abandoned1")
    clock.now += timedelta(hours=25)

    settings = fixture_settings(frontend_dist=str(tmp_path / "x"))
    deps = make_deps(settings, store=store)
    deps.checkpointer = saver
    app = create_app(settings, deps)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if await saver.aget_tuple(cfg) is None:
                break
            await asyncio.sleep(0.01)
        assert await saver.aget_tuple(cfg) is None
        assert await store.stale_paused_threads(-1) == []
    await store.close()
