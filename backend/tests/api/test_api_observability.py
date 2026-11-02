"""JSON logging, Sentry gating and LangSmith env gating."""

from __future__ import annotations

import json
import logging

import pytest

from app import observability
from app.deps import fixture_settings


def test_json_formatter_includes_context_ids_and_extras() -> None:
    record = logging.LogRecord("app.api", logging.INFO, __file__, 1, "plan finished", None, None)
    record.status = "complete"
    rtoken = observability.request_id_var.set("q_123")
    utoken = observability.run_id_var.set("r_456")
    try:
        line = json.loads(observability.JsonFormatter().format(record))
    finally:
        observability.request_id_var.reset(rtoken)
        observability.run_id_var.reset(utoken)
    assert line["msg"] == "plan finished" and line["level"] == "info" and line["logger"] == "app.api"
    assert line["request_id"] == "q_123" and line["run_id"] == "r_456" and line["status"] == "complete"


def test_json_formatter_exception_has_no_traceback() -> None:
    try:
        raise ValueError("bad thing")
    except ValueError:
        import sys

        record = logging.LogRecord("app", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    line = json.loads(observability.JsonFormatter().format(record))
    assert line["error"] == "ValueError: bad thing"
    assert "Traceback" not in json.dumps(line)


def test_tracing_disabled_by_default() -> None:
    env: dict[str, str] = {"LANGSMITH_API_KEY": "k", "LANGSMITH_TRACING": "true"}
    applied = observability.configure_tracing(fixture_settings(), environ=env)
    assert env["LANGSMITH_TRACING"] == "false" and env["LANGSMITH_TRACING_V2"] == "false"
    assert "LANGSMITH_HIDE_OUTPUTS" not in applied


def test_tracing_needs_a_key() -> None:
    env: dict[str, str] = {}
    observability.configure_tracing(fixture_settings(tracing_enabled=True), environ=env)
    assert env["LANGSMITH_TRACING"] == "false"


def test_tracing_prod_hides_inputs_and_outputs() -> None:
    env: dict[str, str] = {"LANGSMITH_API_KEY": "k"}
    observability.configure_tracing(fixture_settings(tracing_enabled=True, app_env="prod"), environ=env)
    assert env["LANGSMITH_TRACING"] == "true" and env["LANGSMITH_TRACING_V2"] == "true"
    assert env["LANGSMITH_HIDE_INPUTS"] == "true" and env["LANGSMITH_HIDE_OUTPUTS"] == "true"
    assert env["LANGSMITH_PROJECT"] == "saturday-planner-prod"


def test_tracing_dev_traces_in_full_and_keeps_project() -> None:
    env: dict[str, str] = {"LANGCHAIN_API_KEY": "k", "LANGSMITH_PROJECT": "mine"}
    observability.configure_tracing(fixture_settings(tracing_enabled=True, app_env="dev"), environ=env)
    assert env["LANGSMITH_HIDE_OUTPUTS"] == "false" and env["LANGSMITH_PROJECT"] == "mine"


def test_sentry_only_with_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.setattr(observability, "_sentry_ready", False)
    assert observability.init_sentry(fixture_settings()) is False
    assert calls == []
    assert observability.init_sentry(fixture_settings(sentry_dsn="https://key@o0.ingest.sentry.io/1",
                                                      git_sha="abc")) is True
    assert calls[0]["send_default_pii"] is False and calls[0]["release"] == "abc"
    assert calls[0]["max_request_body_size"] == "never" and calls[0]["include_local_variables"] is False


def test_validation_error_logs_do_not_echo_input() -> None:
    import sys

    from app.domain.schemas import PlanRequest

    try:
        PlanRequest.model_validate({"mood": "my secret mood text", "budget": -12345, "group_size": 99})
    except Exception:
        record = logging.LogRecord("app", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    line = observability.JsonFormatter().format(record)
    assert "ValidationError: 2 error(s) in PlanRequest" in line
    assert "-12345" not in line and "input_value" not in line


def test_sentry_scrubber_drops_bodies_and_sensitive_headers() -> None:
    event = {"request": {"data": "free text", "cookies": {"a": "b"}, "query_string": "q=1",
                         "headers": {"Authorization": "x", "X-Forwarded-For": "1.2.3.4", "Accept": "*/*"}}}
    out = observability._scrub_event(event, {})
    assert out is not None
    assert out["request"] == {"headers": {"Accept": "*/*"}}
