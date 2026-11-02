"""Runner: skip logic for unavailable models, JSONL records, clarification resume, exit status (offline)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from evals import runner
from evals.assertions import Check
from evals.runner import parse_args, run, sanitize, unavailable_reason

OLLAMA = "http://ollama.test:11434"
REQUIRED = {"case", "split", "suite", "repeat", "model", "kind", "status", "mode", "expected_statuses", "assertions",
            "finalize", "usage", "latency_ms", "error", "git_sha", "prompt_hash", "registry_hash", "fixture_hash",
            "timestamp", "session", "hard_pass", "clarification", "error_categories"}


def test_sanitize_model_ids() -> None:
    assert sanitize("ollama:granite4:tiny-h") == "ollama_granite4_tiny-h"
    assert sanitize("google_genai:<pending>") == "google_genai_pending"


def test_pending_unknown_and_keyless_models_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "pending" in (unavailable_reason("xai:<pin after listing models with the key>", {}) or "")
    assert "pending" in (unavailable_reason("xai:grok-anything", {}) or "")
    assert "not runnable" in (unavailable_reason("nope:model", {}) or "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert unavailable_reason("anthropic:claude-opus-5-5", {}) == "ANTHROPIC_API_KEY is not set"
    assert unavailable_reason("anthropic:claude-opus-5-5", {"anthropic_api_key": "sk-test"}) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert unavailable_reason("anthropic:claude-opus-5-5", {}) is None
    assert unavailable_reason("baseline:deterministic", {}) is None
    assert unavailable_reason("fake:agent", {}) is None


@respx.mock
def test_ollama_down_or_model_missing_is_skipped() -> None:
    route = respx.get(f"{OLLAMA}/api/tags")
    route.mock(side_effect=httpx.ConnectError("refused"))
    assert "not reachable" in (unavailable_reason("ollama:granite4:tiny-h", {"ollama_base_url": OLLAMA}) or "")
    route.mock(return_value=httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}]}))
    reason = unavailable_reason("ollama:granite4:tiny-h", {"ollama_base_url": OLLAMA})
    assert reason is not None and "not pulled" in reason and "ollama pull granite4:tiny-h" in reason
    route.mock(return_value=httpx.Response(200, json={"models": [{"name": "granite4:tiny-h"}]}))
    assert unavailable_reason("ollama:granite4:tiny-h", {"ollama_base_url": OLLAMA}) is None
    route.mock(return_value=httpx.Response(200, json={"models": [{"name": "my-model:latest"}]}))
    assert unavailable_reason("ollama:my-model", {"ollama_base_url": OLLAMA}) is None


def read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def only_file(out: Path, name: str) -> Path:
    files = list(out.glob(f"*/{name}"))
    assert len(files) == 1, files
    return files[0]


async def test_offline_run_writes_one_record_per_run(tmp_path: Path) -> None:
    args = parse_args(["--suite", "offline", "--cases", "assignment_example,clarification_answered,negative_budget",
                       "--out", str(tmp_path)])
    assert await run(args) == 0
    recs = read(only_file(tmp_path, "fake_agent.jsonl"))
    assert [r["case"] for r in recs] == ["assignment_example", "clarification_answered", "negative_budget"]
    for r in recs:
        assert set(r) >= REQUIRED, REQUIRED - set(r)
        assert r["record_type"] == "run" and r["suite"] == "offline" and r["kind"] == "fake"
        assert r["hard_pass"] is True, [a for a in r["assertions"] if a["passed"] is False]
        assert all(a["kind"] in {"code", "judge"} for a in r["assertions"])
        assert len(r["prompt_hash"]) == 12 and len(r["fixture_hash"]) == 12
    answered = recs[1]
    assert [p["status"] for p in answered["phases"]] == ["needs_clarification", "complete"]
    assert answered["phases"][1]["resumed"] is True
    assert answered["clarification"] == [{"phase": 1, "blocking": True, "asked": True},
                                         {"phase": 2, "blocking": False, "asked": False}]
    assert answered["usage"]["llm_calls"] >= 2  # both phases are summed
    rejected = recs[2]
    assert rejected["status"] == "rejected" and rejected["http"]["status_code"] == 400
    assert rejected["http"]["planner_runs"] == 0 and rejected["provider_calls"] == {}


async def test_unavailable_models_get_a_skip_record_not_a_failure(tmp_path: Path) -> None:
    args = parse_args(["--suite", "offline", "--models", "xai:<pending>,fake:agent", "--cases",
                       "negative_budget", "--out", str(tmp_path)])
    assert await run(args) == 0
    skip = read(only_file(tmp_path, "xai_pending.jsonl"))
    assert len(skip) == 1 and skip[0]["record_type"] == "skip" and "pending" in skip[0]["reason"]
    assert read(only_file(tmp_path, "fake_agent.jsonl"))[0]["hard_pass"] is True


async def test_offline_gate_fails_on_a_hard_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "evaluate", lambda obs: [Check("injected", False, "synthetic failure")])
    base = ["--suite", "offline", "--cases", "negative_budget", "--out", str(tmp_path)]
    assert await run(parse_args(base)) == 1
    assert await run(parse_args([*base, "--no-gate"])) == 0
    models = ["--suite", "models", "--models", "baseline:deterministic", "--cases", "vague_free_text", "--out",
              str(tmp_path)]
    assert await run(parse_args(models)) == 0  # the models suite reports, it does not gate by default
    assert await run(parse_args([*models, "--gate"])) == 1


async def test_failed_runs_are_recorded_with_their_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("provider exploded")

    import app.graph.runner as graph_runner

    monkeypatch.setattr(graph_runner, "run_plan", boom)
    args = parse_args(["--suite", "models", "--models", "baseline:deterministic", "--cases", "assignment_example",
                       "--out", str(tmp_path)])
    assert await run(args) == 0
    rec = read(only_file(tmp_path, "baseline_deterministic.jsonl"))[0]
    assert rec["hard_pass"] is False and rec["error"].startswith("harness_error: RuntimeError")
    assert rec["error_categories"] == ["harness_error"]


def test_parse_args_validation() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--repeats", "0"])
    args = parse_args([])
    assert args.suite == "offline" and args.repeats == 1 and args.split == "all" and args.gate is None


def test_fmt_duration() -> None:
    assert runner.fmt_duration(0) == "0s"
    assert runner.fmt_duration(59.6) == "1m00s"
    assert runner.fmt_duration(750) == "12m30s"
    assert runner.fmt_duration(3720) == "1h02m"


async def test_resume_skips_recorded_runs_and_joins_the_same_session(tmp_path: Path,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    first = ["--suite", "offline", "--cases", "negative_budget,vague_free_text", "--out", str(tmp_path)]
    assert await run(parse_args(first)) == 0
    more = ["--suite", "offline", "--cases", "negative_budget,vague_free_text,missing_city_api", "--out",
            str(tmp_path), "--resume"]
    assert await run(parse_args(more)) == 0
    recs = read(only_file(tmp_path, "fake_agent.jsonl"))
    assert sorted(r["case"] for r in recs[:2]) == ["negative_budget", "vague_free_text"]
    assert recs[2]["case"] == "missing_city_api"
    assert len({r["session"] for r in recs}) == 1  # the resumed run joined the earlier session
    out = capsys.readouterr().out
    assert "2 run(s) already recorded; 1 run(s) to do" in out
    assert await run(parse_args(more)) == 0  # nothing left: nothing rerun
    assert len(read(only_file(tmp_path, "fake_agent.jsonl"))) == 3


async def test_resume_never_reruns_failed_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = ["--suite", "offline", "--cases", "negative_budget", "--out", str(tmp_path), "--no-gate"]
    with monkeypatch.context() as m:
        m.setattr(runner, "evaluate", lambda obs: [Check("injected", False, "synthetic failure")])
        assert await run(parse_args(args)) == 0
    assert await run(parse_args([*args, "--resume"])) == 0
    recs = read(only_file(tmp_path, "fake_agent.jsonl"))
    assert len(recs) == 1 and recs[0]["hard_pass"] is False


async def test_resume_starts_a_new_session_when_provenance_differs(tmp_path: Path) -> None:
    args = ["--suite", "offline", "--cases", "negative_budget", "--out", str(tmp_path)]
    assert await run(parse_args(args)) == 0
    path = only_file(tmp_path, "fake_agent.jsonl")
    rec = read(path)[0]
    rec["git_sha"] = "0000000older"
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    point = runner.resume_point(tmp_path, "offline", "fake:agent", {k: "x" for k in runner.RESUME_KEYS})
    assert point.session is None and "git_sha" in point.note
    assert await run(parse_args([*args, "--resume"])) == 0
    recs = read(path)
    assert len(recs) == 2 and recs[0]["session"] != recs[1]["session"]


async def test_max_minutes_stops_and_resume_finishes(tmp_path: Path) -> None:
    base = ["--suite", "offline", "--cases", "negative_budget,vague_free_text", "--out", str(tmp_path)]
    assert await run(parse_args([*base, "--max-minutes", "0"])) == 3
    recs = read(only_file(tmp_path, "fake_agent.jsonl"))
    assert [r["record_type"] for r in recs] == ["budget_stop"] and recs[0]["remaining"] == 2
    assert await run(parse_args([*base, "--resume", "--max-minutes", "60"])) == 0
    runs = [r for r in read(only_file(tmp_path, "fake_agent.jsonl")) if r["record_type"] == "run"]
    assert sorted(r["case"] for r in runs) == ["negative_budget", "vague_free_text"]
    assert runs[0]["session"] == recs[0]["session"]  # continued the stopped session


def test_ctrl_c_exits_130_with_a_resume_hint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    async def interrupted(args: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "run", interrupted)
    monkeypatch.setattr(runner, "_configure_logging", lambda verbose: None)
    assert runner.main(["--suite", "offline"]) == 130
    assert "--resume" in capsys.readouterr().err


def test_pace_s_flag_parses_and_rejects_negative() -> None:
    assert parse_args(["--suite", "offline", "--pace-s", "15"]).pace_s == 15.0
    assert parse_args(["--suite", "offline"]).pace_s == 0.0
    with pytest.raises(SystemExit):
        parse_args(["--suite", "offline", "--pace-s", "-1"])
