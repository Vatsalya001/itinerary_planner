"""Independent service audit: privacy reproductions across the API and the runner (AUD-S10..).

``xfail(strict=True)``: each test fails today and must be un-marked once the defect is fixed.
Offline only: fixture providers, the scripted fake model, in-memory store and checkpointer.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
import pytest

import app.main as main_module
from app.deps import build_deps, fixture_settings
from app.main import create_app

RESUME_PATH = re.compile(r"^/api/plan/([^/]+)/resume$")
PRIVATE_MOOD = "nervous about my divorce hearing, want something calming"


@pytest.fixture(autouse=True)
def _quiet_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "setup_observability", lambda settings: {})


async def test_aud_s10_access_log_never_holds_a_usable_resume_token(tmp_path: Any,
                                                                   caplog: pytest.LogCaptureFixture) -> None:
    settings = fixture_settings(primary_model="fake:agent", frontend_dist=str(tmp_path / "no-dist"))
    app = create_app(settings, build_deps(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4321))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            paused = (await client.post("/api/plan", json={"free_text": "plan something fun for saturday",
                                                           "mood": PRIVATE_MOOD})).json()
            assert paused["status"] == "needs_clarification"
            thread_id = paused["thread_id"]
            with caplog.at_level(logging.INFO, logger="app.http"):
                bad = await client.post(f"/api/plan/{thread_id}/resume", json={"answers": {}})  # user typo: 400
            assert bad.status_code == 400
            logged = [str(getattr(r, "path", "")) for r in caplog.records if r.name == "app.http"]
            tokens = [m.group(1) for p in logged if (m := RESUME_PATH.match(p))]
            exposed = None
            if tokens:  # someone who can read the logs replays the token while the thread is still paused
                replay = (await client.post(f"/api/plan/{tokens[0]}/resume", json={
                    "answers": {"city": "Bangalore", "budget_time": "3 hours, 1000"}})).json()
                exposed = (replay.get("preferences") or {}).get("mood")
    assert thread_id not in tokens and exposed != PRIVATE_MOOD, (logged, exposed)
