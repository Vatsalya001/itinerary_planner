"""Share links (GET /api/plans/{id}), redaction + re-hydration through the API, feedback."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.api import routes
from app.store.redact import UNAVAILABLE_NAME, UNAVAILABLE_WARNING
from tests.api.plan_factories import (
    GOOGLE_ID,
    GOOGLE_NAME,
    GOOGLE_PLAN_B_ID,
    GOOGLE_PLAN_B_NAME,
    MOOD,
    FakePlaces,
    FakeRunner,
    MutableClock,
)

BRIEF = {"city": "Bangalore", "budget": 2000, "available_time": "4 hours", "mood": MOOD}


async def _create(client: Any) -> dict[str, Any]:
    resp = await client.post("/api/plan", json=BRIEF)
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


async def test_share_roundtrip_rehydrates_google_fields(make_client: Any, fake_runner: FakeRunner) -> None:
    places = FakePlaces()
    async with make_client(places=places) as (client, _app, _deps):
        original = await _create(client)
        resp = await client.get(f"/api/plans/{original['share_id']}")
        assert resp.status_code == 200
        shared = resp.json()

        assert shared["share_id"] == original["share_id"]
        assert shared["status"] == original["status"]
        orig_stops, stops = original["plan"]["stops"], shared["plan"]["stops"]
        assert [s["candidate"]["id"] for s in stops] == [s["candidate"]["id"] for s in orig_stops]
        assert [(s["start_min"], s["end_min"]) for s in stops] == [(s["start_min"], s["end_min"]) for s in orig_stops]
        assert shared["plan"]["totals"] == original["plan"]["totals"]
        assert shared["plan"]["legs"] == original["plan"]["legs"]

        cafe = stops[0]["candidate"]
        assert cafe["name"] == GOOGLE_NAME
        assert cafe["address"] == "Fresh address from Places"  # display fields come from the re-fetch
        assert cafe["rating"] == 4.6
        assert cafe["hours"]["status"] == "known"
        assert cafe["maps_url"].startswith("https://")
        assert cafe["cost"] == orig_stops[0]["candidate"]["cost"]  # our computed fields are kept
        assert stops[0]["plan_b"]["name"] == GOOGLE_PLAN_B_NAME
        assert stops[0]["why"] == orig_stops[0]["why"]  # name token restored in our prose
        assert shared["checks"] == original["checks"]
        assert stops[1]["candidate"] == orig_stops[1]["candidate"]  # snapshot stop returned as stored

        assert sorted(cid for cid, _ in places.get_calls) == sorted([GOOGLE_ID, GOOGLE_PLAN_B_ID])
        ctx = places.get_calls[0][1]
        assert ctx.location.id == "fixture:bangalore" and ctx.location.timezone == "Asia/Kolkata"
        assert ctx.date.isoformat() == original["plan"]["date"]

        # Free text never comes back from a share link.
        assert shared["preferences"]["mood"] == ""
        assert shared["preferences"]["soft_preferences"] == []
        assert shared["preferences"]["hard_constraints"][0]["source_text"] == ""
        assert shared["thread_id"] is None
        assert UNAVAILABLE_WARNING not in shared["warnings"]


async def test_share_rehydrate_failure_shows_unavailable(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client(places=FakePlaces(fail=True)) as (client, _app, _deps):
        original = await _create(client)
        shared = (await client.get(f"/api/plans/{original['share_id']}")).json()
        cafe = shared["plan"]["stops"][0]["candidate"]
        assert cafe["name"] == UNAVAILABLE_NAME
        assert cafe["maps_url"] == (
            "https://www.google.com/maps/search/?api=1&query=Google&query_place_id=ChIJquietleaf0001"
        )
        assert cafe["address"] is None and cafe["rating"] is None
        assert GOOGLE_NAME not in shared["plan"]["stops"][0]["why"]
        assert "this place" in shared["plan"]["stops"][0]["why"]
        assert UNAVAILABLE_WARNING in shared["warnings"]
        assert shared["plan"]["stops"][1]["candidate"]["name"] == "Cubbon Park"


async def test_share_rehydrate_missing_place(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client(places=FakePlaces(missing=True)) as (client, _app, _deps):
        original = await _create(client)
        shared = (await client.get(f"/api/plans/{original['share_id']}")).json()
        assert shared["plan"]["stops"][0]["plan_b"]["name"] == UNAVAILABLE_NAME


async def test_unknown_invalid_and_expired_share_ids_are_404(make_client: Any, fake_runner: FakeRunner,
                                                             clocked_store: Any, clock: MutableClock) -> None:
    async with make_client(store=clocked_store, share_ttl_days=30) as (client, _app, _deps):
        for share_id in ("doesnotexist", "bad!", "x" * 80):
            resp = await client.get(f"/api/plans/{share_id}")
            assert resp.status_code == 404
            assert resp.json() == {"detail": "Plan not found or expired"}

        share_id = (await _create(client))["share_id"]
        assert (await client.get(f"/api/plans/{share_id}")).status_code == 200
        clock.now += timedelta(days=30, seconds=1)
        resp = await client.get(f"/api/plans/{share_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Plan not found or expired"


async def test_feedback(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, deps):
        share_id = (await _create(client))["share_id"]
        resp = await client.post(f"/api/plans/{share_id}/feedback", json={"rating": "up"})
        assert resp.status_code == 204 and resp.content == b""
        resp = await client.post(f"/api/plans/{share_id}/feedback", json={"rating": "down", "reason": "too far"})
        assert resp.status_code == 204
        assert await deps.store.list_feedback(share_id) == [
            {"rating": "up", "reason": None}, {"rating": "down", "reason": "too far"},
        ]

        resp = await client.post("/api/plans/unknownid/feedback", json={"rating": "up"})
        assert resp.status_code == 404

        for bad in ({"rating": "meh"}, {"rating": "up", "reason": "x" * 301}, {"rating": "up", "extra": 1}, {}):
            resp = await client.post(f"/api/plans/{share_id}/feedback", json=bad)
            assert resp.status_code == 400, bad
            assert resp.json()["status"] == "rejected"


async def test_share_views_and_feedback_are_rate_limited(make_client: Any, fake_runner: FakeRunner,
                                                         clocked_store: Any, monkeypatch: pytest.MonkeyPatch
                                                         ) -> None:
    monkeypatch.setattr(routes, "READS_PER_10MIN", 2)
    places = FakePlaces()
    async with make_client(store=clocked_store, places=places) as (client, _app, _deps):
        share_id = (await _create(client))["share_id"]
        assert (await client.get(f"/api/plans/{share_id}")).status_code == 200
        assert (await client.post(f"/api/plans/{share_id}/feedback", json={"rating": "up"})).status_code == 204
        calls_before = len(places.get_calls)
        resp = await client.get(f"/api/plans/{share_id}")
        assert resp.status_code == 429 and resp.headers["retry-after"] == "600"
        assert len(places.get_calls) == calls_before  # no Place Details calls once limited
        assert (await client.post("/api/plan", json=BRIEF)).status_code == 200  # plan limits are separate
