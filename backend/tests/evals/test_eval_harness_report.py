"""Report generation on a tiny synthetic results directory (summary.json contract + EVAL_REPORT.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evals import REPO_ROOT
from evals.metrics import FRONTEND_FIELDS, REPORT_ROW_FIELDS
from evals.report import build_summary, load_records, main, render_markdown, select_latest
from tests.evals.helpers import record

OLD, NEW = "20261106T000000Z-old", "20261107T000000Z-new"


def write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def results_dir(tmp_path: Path) -> Path:
    root = tmp_path / "results"
    base: dict[str, Any] = dict(model="baseline:deterministic", kind="baseline", mode="deterministic",
                                first_submit_valid=None, submit_attempts=0)
    write(root / "2026-11-06" / "baseline_deterministic.jsonl",
          [record("a", session_id=OLD, passed=False, **base)])  # superseded by the newer session
    write(root / "2026-11-07" / "baseline_deterministic.jsonl", [
        record("a", session_id=NEW, category="feasible", **base),
        record("h", session_id=NEW, split="holdout", category="weather", passed=False, **base),
    ])
    write(root / "2026-11-07" / "ollama_granite4_tiny-h.jsonl", [
        record("a", model="ollama:granite4:tiny-h", session_id=NEW, latency_ms=25000, status="fallback",
               mode="deterministic", passed=False, error_categories=["model_timeout"]),
    ])
    write(root / "2026-11-07" / "google_genai_pending.jsonl", [
        {"record_type": "skip", "session": NEW, "suite": "models", "model": "google_genai:<pending>", "kind": "api",
         "reason": "pending registry ID", "timestamp": "2026-11-07T00:00:00+00:00"},
    ])
    write(root / "2026-11-07" / "fake_agent.jsonl", [
        record("a", model="fake:agent", kind="fake", suite="offline", session_id=NEW),
    ])
    write(root / "tmp" / "2026-11-07" / "scratch.jsonl", [record("zzz", model="scratch:model", session_id=NEW)])
    (root / "2026-11-07" / "broken.jsonl").write_text("{not json\n", encoding="utf-8")
    return root


def test_load_and_select_latest_session(tmp_path: Path) -> None:
    records, warnings = load_records(results_dir(tmp_path))
    assert any("not JSON" in w for w in warnings)
    assert not any(r.get("model") == "scratch:model" for r in records)  # scratch dirs are ignored
    runs, skips = select_latest(records)
    assert [r["session"] for r in runs[("models", "baseline:deterministic")]] == [NEW, NEW]
    assert [s["model"] for s in skips] == ["google_genai:<pending>"]


def test_summary_matches_the_frontend_contract(tmp_path: Path) -> None:
    records, _ = load_records(results_dir(tmp_path))
    summary = build_summary(records, generated_at="2026-11-07T12:00:00+00:00")
    assert summary["generated_at"] == "2026-11-07T12:00:00+00:00" and summary["git_sha"] == "abc123"
    rows = summary["rows"]
    assert [r["model"] for r in rows] == ["baseline:deterministic", "ollama:granite4:tiny-h"]  # baseline first
    for row in rows:
        assert set(FRONTEND_FIELDS) | set(REPORT_ROW_FIELDS) <= set(row)
        assert row["kind"] in {"baseline", "api", "local"}
        for key in ("runs", "latency_p50_ms", "latency_p95_ms"):
            assert row[key] is None or isinstance(row[key], int)
        for key in FRONTEND_FIELDS[3:]:
            assert row[key] is None or isinstance(row[key], int | float)
    base = rows[0]
    assert base["runs"] == 2 and base["hard_pass_rate"] == 0.5 and base["agent_success"] is None
    assert base["splits"]["dev"]["hard_passes"] == 1 and base["splits"]["holdout"]["hard_passes"] == 0
    granite = rows[1]
    assert granite["latency_p50_ms"] == 25000 and granite["error_categories"] == {"model_timeout": 1}
    # categories: flat objects of rates in 0..1 keyed by model, so the page's table treats them as rates
    for cat, per_model in summary["categories"].items():
        assert all(v is None or 0 <= v <= 1 for v in per_model.values()), cat
    assert summary["categories"]["weather"] == {"baseline:deterministic": 0.0}
    assert summary["categories_n"]["feasible"] == {"baseline:deterministic": 1, "ollama:granite4:tiny-h": 1}
    assert summary["suites"]["offline"]["rows"][0]["model"] == "fake:agent"  # never mixed into the leaderboard
    assert summary["skipped"][0]["reason"] == "pending registry ID"
    assert {f["model"] for f in summary["failures"]} == {"baseline:deterministic", "ollama:granite4:tiny-h"}
    assert summary["judge"] is None
    assert summary["superseded_sessions"] == [{"suite": "models", "model": "baseline:deterministic", "session": OLD,
                                               "runs": 1, "relation": "older"}]


def test_markdown_numbers_come_from_the_records(tmp_path: Path) -> None:
    records, _ = load_records(results_dir(tmp_path))
    md = render_markdown(build_summary(records))
    assert "| `baseline:deterministic` | baseline |" in md
    assert "50.0% (1/2)" in md  # baseline hard pass
    assert "## Skipped models" in md and "google_genai:<pending>" in md
    assert "not model quality" in md and "No judge model ran" in md
    assert "`h` #1 (holdout, weather)" in md  # failure example with case, split and category
    assert "model_timeout ×1" in md


def test_main_writes_summary_and_report(tmp_path: Path) -> None:
    root = results_dir(tmp_path)
    report = tmp_path / "docs" / "EVAL_REPORT.md"
    assert main(["--results", str(root), "--report", str(report)]) == 0
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    assert {"generated_at", "git_sha", "rows", "categories"} <= set(summary)
    assert report.read_text(encoding="utf-8").startswith("# Eval report")


def test_empty_results_directory(tmp_path: Path) -> None:
    summary = build_summary([])
    assert summary["rows"] == [] and summary["categories"] == {}
    assert "No `models` suite results yet" in render_markdown(summary)
    assert load_records(tmp_path / "missing")[0] == []


def test_row_fields_cover_the_frontend_eval_row_type() -> None:
    types = REPO_ROOT / "frontend" / "src" / "types.ts"
    if not types.is_file():
        return
    text = types.read_text(encoding="utf-8")
    block = re.search(r"export interface EvalRow \{(.*?)\n\}", text, re.S)
    assert block is not None
    fields = set(re.findall(r"^\s+(\w+)\?:", block.group(1), re.M))
    assert fields == set(FRONTEND_FIELDS) | set(REPORT_ROW_FIELDS)


def test_budget_stop_is_reported_while_the_session_is_partial(tmp_path: Path) -> None:
    root = tmp_path / "results"
    stop = {"record_type": "budget_stop", "session": NEW, "suite": "models", "model": "ollama:granite4:tiny-h",
            "kind": "local", "max_minutes": 30.0, "remaining": 25, "timestamp": "2026-11-07T01:00:00+00:00"}
    write(root / "2026-11-07" / "ollama_granite4_tiny-h.jsonl",
          [record("assignment_example", model="ollama:granite4:tiny-h", session_id=NEW), stop])
    summary = build_summary(load_records(root)[0])
    assert summary["rows"][0]["coverage"]["partial"] is True
    assert summary["stopped_early"] == [{"suite": "models", "model": "ollama:granite4:tiny-h", "session": NEW,
                                         "reason": "max_minutes", "max_minutes": 30.0, "max_cost_usd": None,
                                         "spent_usd": None, "remaining": 25,
                                         "timestamp": "2026-11-07T01:00:00+00:00"}]
    md = render_markdown(summary)
    assert "Stopped early by `--max-minutes`" in md and "25 run(s) left after a 30-minute budget" in md
    assert "(partial)" in md


def test_a_newer_partial_session_does_not_hide_the_last_complete_one(tmp_path: Path) -> None:
    root = tmp_path / "results"
    full = [record(c, model="m:1", session_id=OLD) for c in ("a", "b", "c")]
    partial = [record("a", model="m:1", session_id=NEW, passed=False)]
    write(root / "2026-11-07" / "m_1.jsonl", full + partial)
    records = load_records(root)[0]
    runs, _ = select_latest(records, {"models": 3})
    assert {r["session"] for r in runs[("models", "m:1")]} == {OLD}
    runs, _ = select_latest(records, {"models": 4})  # nothing complete: the latest session is shown
    assert {r["session"] for r in runs[("models", "m:1")]} == {NEW}
    runs, _ = select_latest(records)
    assert {r["session"] for r in runs[("models", "m:1")]} == {NEW}


def test_rows_with_older_prompts_are_marked() -> None:
    from evals.report import mark_prompt_versions

    rows = [{"model": "ollama:a", "kind": "local", "provenance": {"prompt_hash": "old"}},
            {"model": "cerebras:x", "kind": "api", "provenance": {"prompt_hash": "new"}},
            {"model": "baseline:deterministic", "kind": "baseline", "provenance": {"prompt_hash": "old"}}]
    mark_prompt_versions(rows, "new")
    assert [r["prompt_current"] for r in rows] == [False, True, True]
