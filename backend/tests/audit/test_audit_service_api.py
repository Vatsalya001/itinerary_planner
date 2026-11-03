"""Independent audit (service layer): API, HTTP security/privacy and deploy-config reproductions.

Every test here reproduces a confirmed defect and is marked ``xfail(strict=True)``: it fails today and turns into
an XPASS (a CI failure) once the defect is fixed, so the marker must then be removed. Offline only: fixture
providers, the scripted fake model and in-memory stores; no network, no keys.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

import app.main as main_module
from app.deps import Deps, build_deps, fixture_settings
from app.domain.schemas import Location
from app.main import create_app
from app.store.sqlite import SQLiteStore
from tests.api.plan_factories import FakePlaces, make_deps

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIEF = {"city": "Bangalore", "budget": 2000, "available_time": "4 hours",
         "mood": "tired but wants to do something fun", "interests": ["food", "music", "walks"],
         "constraints": ["vegetarian", "avoid crowded places"]}
MARKER = "zzusersecretzz"  # stands in for user-typed text; must never come back in an error body


@pytest.fixture(autouse=True)
def _quiet_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lifespan's observability setup touches the root logger and LangSmith env; keep tests hermetic.
    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})


@asynccontextmanager
async def api_client(tmp_path: Path, *, deps: Deps | None = None, with_dist: bool = False,
                     **overrides: Any) -> AsyncIterator[tuple[httpx.AsyncClient, Deps]]:
    """The real app (lifespan included) over ASGI. ``with_dist`` serves a minimal built SPA from ``tmp_path``."""
    if with_dist:
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<!doctype html><title>spa</title>", encoding="utf-8")
        overrides["frontend_dist"] = str(dist)
    else:
        overrides.setdefault("frontend_dist", str(tmp_path / "no-dist"))
    settings = fixture_settings(**overrides)
    if deps is None:
        deps = make_deps(settings, places=FakePlaces())
    app = create_app(settings, deps)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4321))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, deps


# ---------------------------------------------------------------------------------------------------------
# AUD-S20: 400 responses echo user input
# ---------------------------------------------------------------------------------------------------------


async def test_validation_errors_never_echo_user_input(tmp_path: Path) -> None:
    async with api_client(tmp_path) as (client, _deps):
        bodies = [
            ("/api/plan", {**BRIEF, MARKER: True}),  # unknown field: its name is user text
            ("/api/plan", {**BRIEF, "relaxed_constraints": [f"diet:{MARKER}"]}),  # ValueError echoes the key
            ("/api/plan/t_abcdef12/resume", {"answers": {f"{MARKER} key!": "Pune"}}),  # dict key in loc
        ]
        leaked = []
        for path, body in bodies:
            resp = await client.post(path, json=body)
            assert resp.status_code == 400
            if MARKER in resp.text:
                leaked.append((path, resp.json()["detail"]))
    # HLD section 10: "400 with field messages that never echo values".
    assert leaked == []


# ---------------------------------------------------------------------------------------------------------
# AUD-S21: a NUL byte in the path is an unhandled 500 (ERROR log + Sentry event per request)
# ---------------------------------------------------------------------------------------------------------


async def test_nul_byte_in_path_is_not_a_server_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="app.http")
    async with api_client(tmp_path, with_dist=True) as (client, _deps):
        statuses = [(await client.get(path)).status_code for path in ("/%00", "/assets/x%00.js", "/plans/a%00b")]
    unhandled = [r for r in caplog.records if r.getMessage() == "unhandled error"]
    # Garbage paths should get the SPA (or a 404), not an error that is logged at ERROR and sent to Sentry.
    assert all(code < 500 for code in statuses), statuses
    assert unhandled == []


# ---------------------------------------------------------------------------------------------------------
# AUD-S22: HEAD /api/health is not served
# ---------------------------------------------------------------------------------------------------------


async def test_head_health_answers_like_get(tmp_path: Path) -> None:
    codes = {}
    for with_dist in (False, True):
        async with api_client(tmp_path / str(with_dist), with_dist=with_dist) as (client, _deps):
            assert (await client.get("/api/health")).status_code == 200
            codes[with_dist] = (await client.head("/api/health")).status_code
    # Uptime monitors commonly probe with HEAD; a healthy service must not look down (404) or broken (405).
    assert codes == {False: 200, True: 200}


# ---------------------------------------------------------------------------------------------------------
# AUD-S23: the manual model-eval CI job cannot run the production models
# ---------------------------------------------------------------------------------------------------------


def test_ci_model_evals_job_gets_the_production_model_keys() -> None:
    ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = ci["jobs"]["model-evals"]
    env = set(job.get("env") or {})
    for step in job["steps"]:
        env |= set(step.get("env") or {})
    render = yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
    service_env = {v["key"]: v.get("value") for v in render["services"][0]["envVars"]}
    production = {service_env["PRIMARY_MODEL"], service_env["FALLBACK_MODEL"]}
    assert production == {"cerebras:gpt-oss-120b", "groq:openai/gpt-oss-120b"}
    # The job exists to evaluate models with repository secrets (HLD 13); the deployed models need these keys.
    assert {"CEREBRAS_API_KEY", "GROQ_API_KEY"} <= env, sorted(env)


# ---------------------------------------------------------------------------------------------------------
# AUD-S24: a rejected concurrent resume forgets the paused thread that the other resume still owns
# ---------------------------------------------------------------------------------------------------------


class SlowGeocoder:
    """Delays lookups so a resume stays in flight while a second resume arrives."""

    def __init__(self, inner: Any, delay_s: float) -> None:
        self.inner = inner
        self.delay_s = delay_s

    async def resolve(self, city: str) -> list[Location]:
        await asyncio.sleep(self.delay_s)
        result: list[Location] = await self.inner.resolve(city)
        return result


async def test_rejected_concurrent_resume_keeps_the_paused_thread_record(tmp_path: Path) -> None:
    settings = fixture_settings(primary_model="fake:agent", frontend_dist=str(tmp_path / "no-dist"))
    deps = build_deps(settings)
    deps.checkpointer = InMemorySaver()
    store = SQLiteStore(":memory:")
    await store.init()
    deps.store = store
    try:
        async with api_client(tmp_path, deps=deps) as (client, _deps):
            paused = (await client.post("/api/plan", json={"free_text": "plan something fun"})).json()
            assert paused["status"] == "needs_clarification"
            thread_id = paused["thread_id"]
            assert thread_id in await store.stale_paused_threads(-3600)  # recorded for retention

            deps.geocoder = SlowGeocoder(deps.geocoder, 1.0)  # the resume re-geocodes the answered city
            answers = {"answers": {"city": "Bangalore", "budget_time": "4 hours, 1500"}}
            first = asyncio.create_task(client.post(f"/api/plan/{thread_id}/resume", json=answers))
            await asyncio.sleep(0.3)
            second = (await client.post(f"/api/plan/{thread_id}/resume", json=answers)).json()
            assert second["status"] == "rejected" and "already being resumed" in second["message"]
            # The thread is still paused/in flight and owned by the first call: its retention record must stay,
            # or a crash/redeploy during that resume leaves a checkpoint (raw user request) no sweep will delete.
            still_tracked = thread_id in await store.stale_paused_threads(-3600)
            assert (await first).json()["status"] == "complete"
        assert still_tracked
    finally:
        await store.close()
