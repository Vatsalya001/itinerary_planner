"""POST /api/plan and /api/plan/{thread_id}/resume: SSE, JSON, validation, limits, persistence."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.api import routes
from tests.api.plan_factories import (
    CONSTRAINT_TEXT,
    GOOGLE_ID,
    MOOD,
    FakeRunner,
    make_result,
    parse_sse,
)

SSE = {"Accept": "text/event-stream"}
JSON_ACCEPT = {"Accept": "application/json"}
BRIEF = {
    "city": "Bangalore", "budget": 2000, "available_time": "4 hours", "mood": MOOD,
    "interests": ["food", "music", "walks"], "constraints": ["vegetarian", "avoid crowded places"],
}


async def test_sse_framing_order_and_share_id(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, deps):
        resp = await client.post("/api/plan", json=BRIEF, headers=SSE)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        assert resp.text.endswith("\n\n")
        events = parse_sse(resp.text)
        types = [e["type"] for e in events]
        assert types == ["run_started", "node_started", "node_completed", "tool_called", "tool_completed",
                         "validation", "node_started", "node_completed", "result"]
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        run_id = fake_runner.calls[0]["run_id"]
        assert run_id and all(e["run_id"] == run_id for e in events)
        assert fake_runner.calls[0]["request"].city == "Bangalore"

        result = events[-1]["data"]
        assert result["status"] == "complete"
        share_id = result["share_id"]
        assert share_id and len(share_id) >= 6
        assert result["latency_ms"] is not None
        assert await deps.store.get_plan(share_id) is not None


async def test_sse_run_record_uses_finalize_data(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, deps):
        resp = await client.post("/api/plan", json=BRIEF, headers=SSE)
        run_id = parse_sse(resp.text)[-1]["data"]["run_id"]
        run = await deps.store.get_run(run_id)
        assert run is not None
        assert run.status == "complete" and run.mode == "agent" and run.model == "scripted:fake"
        assert (run.input_tokens, run.output_tokens, run.cost_usd) == (1234, 321, 0.0123)
        assert run.tool_calls == {"search_places": 2, "check_plan": 1}
        assert run.submit_attempts == 2
        assert run.git_sha == deps.settings.git_sha
        dumped = run.model_dump_json()
        assert MOOD not in dumped and CONSTRAINT_TEXT not in dumped


async def test_heartbeat_during_silence(make_client: Any, monkeypatch: pytest.MonkeyPatch,
                                        fast_heartbeat: None) -> None:
    fake = FakeRunner(delay_before_result=0.12)
    from app.graph import runner as runner_module

    monkeypatch.setattr(runner_module, "stream_plan", fake.stream_plan)
    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", json=BRIEF, headers=SSE)
        events = parse_sse(resp.text)
        types = [e["type"] for e in events]
        assert "heartbeat" in types
        assert types[-1] == "result"
        hb = next(e for e in events if e["type"] == "heartbeat")
        assert hb["run_id"] == events[0]["run_id"]
        assert types.index("heartbeat") > types.index("node_completed")


async def test_json_mode_returns_plan_result(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, deps):
        resp = await client.post("/api/plan", json=BRIEF, headers=JSON_ACCEPT)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["status"] == "complete"
        assert body["share_id"]
        assert fake_runner.run_plan_calls == 1
        assert await deps.store.get_plan(body["share_id"]) is not None
        run = await deps.store.get_run(body["run_id"])
        assert run is not None and run.submit_attempts == 2


async def test_no_accept_header_defaults_to_json(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", json=BRIEF)
        assert resp.status_code == 200
        assert resp.json()["status"] == "complete"


async def test_needs_clarification_is_not_shared(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRunner(result_factory=lambda **kw: make_result(status="needs_clarification",
                                                              thread_id="t_abcdef12", **kw))
    from app.graph import runner as runner_module

    monkeypatch.setattr(runner_module, "stream_plan", fake.stream_plan)
    async with make_client() as (client, _app, deps):
        events = parse_sse((await client.post("/api/plan", json={"free_text": "plan my saturday"},
                                              headers=SSE)).text)
        result = events[-1]["data"]
        assert result["status"] == "needs_clarification"
        assert result["share_id"] is None
        assert result["thread_id"] == "t_abcdef12"
        run = await deps.store.get_run(result["run_id"])
        assert run is not None and run.status == "needs_clarification"


async def test_negative_budget_is_400_without_runner_call(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", json={**BRIEF, "budget": -1}, headers=SSE)
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "rejected"
        assert isinstance(body["detail"], list)
        assert any(msg.startswith("budget:") for msg in body["detail"])
        assert fake_runner.calls == []


async def test_unknown_field_and_bad_json_are_400(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", json={**BRIEF, "admin": True})
        assert resp.status_code == 400 and resp.json()["status"] == "rejected"
        resp = await client.post("/api/plan", content=b"{not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 400 and resp.json()["status"] == "rejected"
        assert fake_runner.calls == []


async def test_oversize_body_with_content_length_is_413(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, _deps):
        body = json.dumps({**BRIEF, "mood": "x" * 20_000}).encode()
        resp = await client.post("/api/plan", content=body,
                                 headers={"Content-Type": "application/json", **SSE})
        assert resp.status_code == 413
        assert resp.json() == {"status": "rejected", "detail": "Request too large"}
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert fake_runner.calls == []


async def test_oversize_chunked_body_without_content_length_is_413(make_client: Any,
                                                                  fake_runner: FakeRunner) -> None:
    payload = json.dumps({**BRIEF, "mood": "y" * 20_000}).encode()

    async def chunks() -> Any:
        for i in range(0, len(payload), 1024):
            yield payload[i:i + 1024]

    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", content=chunks(), headers={"Content-Type": "application/json"})
        assert "content-length" not in {k.lower() for k in resp.request.headers}
        assert resp.status_code == 413
        assert resp.json()["detail"] == "Request too large"
        assert fake_runner.calls == []


async def test_small_chunked_body_is_accepted(make_client: Any, fake_runner: FakeRunner) -> None:
    payload = json.dumps(BRIEF).encode()

    async def chunks() -> Any:
        for i in range(0, len(payload), 16):
            yield payload[i:i + 16]

    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", content=chunks(), headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        assert fake_runner.calls[0]["request"].budget == 2000


async def test_per_ip_rate_limit_returns_429_with_retry_after(make_client: Any, fake_runner: FakeRunner,
        clocked_store: Any) -> None:
    async with make_client(store=clocked_store, rate_limit_per_10min=2) as (client, _app, _deps):
        for _ in range(2):
            assert (await client.post("/api/plan", json=BRIEF)).status_code == 200
        resp = await client.post("/api/plan", json=BRIEF, headers=SSE)
        assert resp.status_code == 429
        retry_after = int(resp.headers["retry-after"])
        assert retry_after == 300  # clock is 12:05 UTC; the 10-minute window resets at 12:10
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["detail"] == f"Too many plans; try again in {retry_after} seconds"
        assert len(fake_runner.calls) == 2  # no model call for the limited request


async def test_per_day_ip_limit(make_client: Any, fake_runner: FakeRunner,
        clocked_store: Any) -> None:
    async with make_client(store=clocked_store, rate_limit_per_10min=10, rate_limit_per_day=1) as (client, _app, _deps):
        assert (await client.post("/api/plan", json=BRIEF)).status_code == 200
        resp = await client.post("/api/plan", json=BRIEF)
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) == 11 * 3600 + 55 * 60  # until 00:00 UTC


async def test_global_daily_cap(make_client: Any, fake_runner: FakeRunner,
        clocked_store: Any) -> None:
    async with make_client(store=clocked_store, daily_plan_cap=1) as (client, _app, _deps):
        assert (await client.post("/api/plan", json=BRIEF)).status_code == 200
        resp = await client.post("/api/plan", json=BRIEF)
        assert resp.status_code == 429
        assert "today" in resp.json()["detail"]
        assert int(resp.headers["retry-after"]) >= 1
        assert len(fake_runner.calls) == 1


async def test_validation_errors_do_not_consume_rate_limit(make_client: Any, fake_runner: FakeRunner,
        clocked_store: Any) -> None:
    async with make_client(store=clocked_store, rate_limit_per_10min=1) as (client, _app, _deps):
        for _ in range(3):
            assert (await client.post("/api/plan", json={"budget": -5})).status_code == 400
        assert (await client.post("/api/plan", json=BRIEF)).status_code == 200


async def test_forwarded_for_only_trusted_when_configured(make_client: Any, fake_runner: FakeRunner,
        clocked_store: Any) -> None:
    async with make_client(store=clocked_store, rate_limit_per_10min=1, trust_proxy_headers=True) as (client, _app, _deps):
        # The proxy appends the real client IP; spoofed leading hops must not create fresh buckets.
        for real in ("198.51.100.1", "198.51.100.2"):
            resp = await client.post("/api/plan", json=BRIEF, headers={"X-Forwarded-For": f"6.6.6.6, {real}"})
            assert resp.status_code == 200
        resp = await client.post("/api/plan", json=BRIEF, headers={"X-Forwarded-For": "7.7.7.7, 198.51.100.1"})
        assert resp.status_code == 429  # same right-most hop, different spoofed first hop
        resp = await client.post("/api/plan", json=BRIEF, headers={"X-Forwarded-For": "198.51.100.2"})
        assert resp.status_code == 429  # single hop is the client
        resp = await client.post("/api/plan", json=BRIEF, headers={"X-Forwarded-For": "1.2.3.4, not-an-ip"})
        assert resp.status_code == 200  # malformed -> peer address (fresh bucket), never the spoofed hop
        resp = await client.post("/api/plan", json=BRIEF, headers={"X-Forwarded-For": "1.2.3.4, garbage"})
        assert resp.status_code == 429  # ...and that peer bucket is now used up

    async with make_client(store=clocked_store, rate_limit_per_10min=1, trust_proxy_headers=False,
                           client_ip="192.0.2.50") as (client, _app, _deps):
        assert (await client.post("/api/plan", json=BRIEF, headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 200
        resp = await client.post("/api/plan", json=BRIEF, headers={"X-Forwarded-For": "2.2.2.2"})
        assert resp.status_code == 429  # spoofed header ignored; same peer IP


async def test_resume_validation(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, _deps):
        bad = [
            ("/api/plan/bad!id/resume", {"answers": {"city": "Bangalore"}}),
            ("/api/plan/abc/resume", {"answers": {"city": "Bangalore"}}),
            ("/api/plan/t_abcdef12/resume", {"answers": {}}),
            ("/api/plan/t_abcdef12/resume", {"answers": {f"q{i}": "x" for i in range(6)}}),
            ("/api/plan/t_abcdef12/resume", {"answers": {"city": "x" * 201}}),
            ("/api/plan/t_abcdef12/resume", {"answers": {"bad key!": "x"}}),
            ("/api/plan/t_abcdef12/resume", {"answers": {"city": "Pune"}, "extra": 1}),
        ]
        for path, body in bad:
            resp = await client.post(path, json=body)
            assert resp.status_code == 400, (path, body, resp.text)
            assert resp.json()["status"] == "rejected"
        assert fake_runner.calls == []


async def test_resume_streams_and_passes_thread_and_answers(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan/t_abcdef12/resume", json={"answers": {"city": "Bangalore"}},
                                 headers=SSE)
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert events[-1]["type"] == "result" and events[-1]["data"]["share_id"]
        call = fake_runner.calls[0]
        assert call["request"] is None
        assert call["thread_id"] == "t_abcdef12"
        assert call["resume_answers"] == {"city": "Bangalore"}

        resp = await client.post("/api/plan/t_abcdef12/resume", json={"answers": {"city": "Mumbai"}})
        assert resp.status_code == 200 and resp.json()["status"] == "complete"
        assert fake_runner.calls[1]["resume_answers"] == {"city": "Mumbai"}


async def test_resume_is_rate_limited(make_client: Any, fake_runner: FakeRunner,
        clocked_store: Any) -> None:
    async with make_client(store=clocked_store, rate_limit_per_10min=1) as (client, _app, _deps):
        assert (await client.post("/api/plan", json=BRIEF)).status_code == 200
        resp = await client.post("/api/plan/t_abcdef12/resume", json={"answers": {"city": "Pune"}})
        assert resp.status_code == 429 and "retry-after" in resp.headers


async def test_xss_strings_are_json_escaped(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRunner(echo_mood=True)
    from app.graph import runner as runner_module

    monkeypatch.setattr(runner_module, "stream_plan", fake.stream_plan)
    monkeypatch.setattr(runner_module, "run_plan", fake.run_plan)
    payload = "<img src=x onerror=alert(1)> chill"
    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", json={**BRIEF, "mood": payload}, headers=SSE)
        assert "<img" not in resp.text and "\\u003cimg" in resp.text
        result = parse_sse(resp.text)[-1]["data"]
        assert result["preferences"]["mood"] == payload  # same string after JSON parsing

        resp = await client.post("/api/plan", json={**BRIEF, "mood": payload}, headers=JSON_ACCEPT)
        assert resp.headers["content-type"].startswith("application/json")
        assert "<img" not in resp.text
        assert resp.json()["preferences"]["mood"] == payload

        resp = await client.post("/api/plan", json={**BRIEF, "mood": payload, "budget": -1})
        assert resp.status_code == 400 and "<img" not in resp.text and "onerror" not in resp.text


async def test_runner_crash_streams_error_then_rejected_result(make_client: Any,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRunner(fail_after=3)
    from app.graph import runner as runner_module

    monkeypatch.setattr(runner_module, "stream_plan", fake.stream_plan)
    async with make_client() as (client, _app, deps):
        resp = await client.post("/api/plan", json=BRIEF, headers=SSE)
        events = parse_sse(resp.text)
        assert [e["type"] for e in events][-2:] == ["error", "result"]
        assert "boom" not in resp.text and "Traceback" not in resp.text
        result = events[-1]["data"]
        assert result["status"] == "rejected" and result["share_id"] is None
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        run = await deps.store.get_run(result["run_id"])
        assert run is not None and run.status == "rejected"


async def test_runner_crash_json_is_generic_500(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom: secret internal detail")

    from app.graph import runner as runner_module

    monkeypatch.setattr(runner_module, "run_plan", broken)
    async with make_client() as (client, _app, _deps):
        resp = await client.post("/api/plan", json=BRIEF, headers=JSON_ACCEPT)
        assert resp.status_code == 500
        body = resp.json()
        assert body["status"] == "rejected" and body["detail"] == "Something went wrong"
        assert body["run_id"].startswith("r_") and body["request_id"]
        assert "boom" not in resp.text


async def test_persistence_failure_does_not_break_response(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, deps):
        async def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("db down")

        deps.store.save_plan = fail
        deps.store.record_run = fail
        resp = await client.post("/api/plan", json=BRIEF, headers=SSE)
        result = parse_sse(resp.text)[-1]["data"]
        assert result["status"] == "complete"
        assert result["share_id"] is None


async def test_rate_limit_store_failure_fails_open(make_client: Any, fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, deps):
        async def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("db down")

        deps.store.hit_rate_limit = fail
        assert (await client.post("/api/plan", json=BRIEF)).status_code == 200


async def test_client_disconnect_still_persists(make_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the ASGI app directly: the client goes away after the first SSE frame."""
    fake = FakeRunner(delay=0.02)
    from app.graph import runner as runner_module

    monkeypatch.setattr(runner_module, "stream_plan", fake.stream_plan)
    monkeypatch.setattr(routes, "new_share_id", lambda: "share_disconnect")
    async with make_client() as (_client, app, deps):
        body = json.dumps(BRIEF).encode()
        disconnected = asyncio.Event()
        sent: list[dict[str, Any]] = []
        receive_calls = 0

        async def receive() -> dict[str, Any]:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/api/plan", "raw_path": b"/api/plan", "query_string": b"",
            "root_path": "", "server": ("testserver", 80), "client": ("198.51.100.77", 5555),
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                        (b"accept", b"text/event-stream"), (b"content-length", str(len(body)).encode())],
            "state": {},
        }
        await app(scope, receive, send)

        frames = [m for m in sent if m["type"] == "http.response.body" and m.get("body")]
        assert 1 <= len(frames) < 9  # the client saw only part of the stream

        await asyncio.wait_for(fake.completed.wait(), timeout=5)
        tasks = list(app.state.background_tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        record = await deps.store.get_plan("share_disconnect")
        assert record is not None
        assert record["result"]["share_id"] == "share_disconnect"
        run = await deps.store.get_run(fake.calls[0]["run_id"])
        assert run is not None and run.status == "complete"


async def test_shared_record_has_no_free_text_or_google_display_fields(make_client: Any,
                                                                      fake_runner: FakeRunner) -> None:
    async with make_client() as (client, _app, deps):
        result = (await client.post("/api/plan", json=BRIEF)).json()
        record = await deps.store.get_plan(result["share_id"])
        raw = json.dumps(record, ensure_ascii=False)
        for forbidden in (MOOD, CONSTRAINT_TEXT, "somewhere quiet", "Quiet Leaf Cafe", "Rainy Day Book Cafe",
                          "12 Example Road", "maps.google.com/?cid"):
            assert forbidden not in raw, forbidden
        assert GOOGLE_ID in raw
        assert "Cubbon Park" in raw  # non-Google candidates are stored as-is
        assert record["redacted"]["google_candidates"] == [GOOGLE_ID, "gp:ChIJbookcafe0002"]
