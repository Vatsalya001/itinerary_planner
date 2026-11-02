"""Harness hardening: judge default, record schema, prompt hash, dirty-tree resume, harness-exception records,
backend/.env loading, cost per plan and --max-cost-usd (offline: fake model, frozen world, no network)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evals import RECORD_SCHEMA, cases
from evals import runner as runner_module
from evals.metrics import model_metrics, record_hard_pass, upgrade_record
from evals.report import build_summary, load_records, render_markdown
from evals.runner import parse_args, run
from tests.evals.helpers import record

NEW = "20261107T000000Z-new"


def read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def only_file(out: Path, name: str) -> Path:
    files = list(out.glob(f"*/{name}"))
    assert len(files) == 1, files
    return files[0]


def write(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def capture_main(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Patch runner.run so main() only resolves its arguments; returns the list the args land in."""
    seen: list[Any] = []

    async def fake_run(args: Any) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(runner_module, "run", fake_run)
    monkeypatch.setattr(runner_module, "_configure_logging", lambda verbose: None)
    return seen


# ---------------------------------------------------------------------------------------------------
# 1. JUDGE_MODEL is the default judge
# ---------------------------------------------------------------------------------------------------


def test_resolve_judge_model_flag_wins_and_none_disables() -> None:
    resolve = runner_module.resolve_judge_model
    assert resolve(None, "models", "anthropic:claude-opus-5-5") == "anthropic:claude-opus-5-5"
    assert resolve(None, "live", " anthropic:claude-opus-5-5 ") == "anthropic:claude-opus-5-5"
    assert resolve("ollama:qwen3.5:9b", "models", "anthropic:claude-opus-5-5") == "ollama:qwen3.5:9b"
    assert resolve("none", "models", "anthropic:claude-opus-5-5") == ""
    assert resolve("NONE", "live", "anthropic:claude-opus-5-5") == ""
    assert resolve("", "models", "anthropic:claude-opus-5-5") == ""
    assert resolve(None, "models", None) == "" and resolve(None, "models", "none") == ""
    # the offline suite (scripted fake plans, no network) is judged only on request
    assert resolve(None, "offline", "anthropic:claude-opus-5-5") == ""
    assert resolve("anthropic:claude-opus-5-5", "offline", None) == "anthropic:claude-opus-5-5"


def test_main_defaults_the_judge_to_the_judge_model_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_main(monkeypatch)
    monkeypatch.setenv("JUDGE_MODEL", "anthropic:claude-opus-5-5")
    assert runner_module.main(["--suite", "models"]) == 0
    assert runner_module.main(["--suite", "models", "--judge-model", "none"]) == 0
    assert runner_module.main(["--suite", "models", "--judge-model", "ollama:gemma4:e4b"]) == 0
    assert runner_module.main(["--suite", "offline"]) == 0
    assert [a.judge_model for a in seen] == ["anthropic:claude-opus-5-5", "", "ollama:gemma4:e4b", ""]
    monkeypatch.delenv("JUDGE_MODEL")
    assert runner_module.main(["--suite", "models"]) == 0
    assert seen[-1].judge_model == ""


def test_judge_model_from_backend_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_main(monkeypatch)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    env = tmp_path / ".env"
    env.write_text("JUDGE_MODEL=anthropic:claude-opus-5-5\n", encoding="utf-8")
    monkeypatch.setattr(runner_module, "ENV_FILE", env)
    assert runner_module.main(["--suite", "models"]) == 0
    assert seen[-1].judge_model == "anthropic:claude-opus-5-5"


async def test_configured_judge_is_used_by_the_run(tmp_path: Path) -> None:
    # A pending judge ID is unavailable: every record carries the "unavailable" judge stub, proving it was set up.
    args = parse_args(["--suite", "models", "--models", "baseline:deterministic", "--cases", "assignment_example",
                       "--out", str(tmp_path)])
    args.judge_model = runner_module.resolve_judge_model(args.judge_model, args.suite, "google_genai:<pending>")
    assert await run(args) == 0
    rec = read(only_file(tmp_path, "baseline_deterministic.jsonl"))[0]
    assert rec["judge"]["status"] == "unavailable" and rec["judge"]["model"] == "google_genai:<pending>"


# ---------------------------------------------------------------------------------------------------
# 2. Record schema bump and older records
# ---------------------------------------------------------------------------------------------------


def schema1_record(case: str = "a", **kw: Any) -> dict[str, Any]:
    rec = record(case, **kw)
    rec["schema"] = 1
    for key in ("harness_hash", "preferences", "message"):
        rec.pop(key)
    return rec


def test_record_schema_is_2_and_runs_record_it() -> None:
    assert RECORD_SCHEMA == 2


async def test_new_records_carry_the_current_schema_and_its_fields(tmp_path: Path) -> None:
    args = parse_args(["--suite", "offline", "--cases", "assignment_example", "--out", str(tmp_path)])
    assert await run(args) == 0
    rec = read(only_file(tmp_path, "fake_agent.jsonl"))[0]
    assert rec["schema"] == RECORD_SCHEMA
    assert {"harness_hash", "preferences", "message", "git_dirty"} <= set(rec)


def test_upgrade_record_fills_schema_1_gaps_and_rejects_newer_schemas() -> None:
    old = schema1_record()
    up = upgrade_record(old)
    assert up is not None and up["schema"] == 1
    assert up["harness_hash"] is None and up["preferences"] is None and up["message"] is None
    assert "harness_hash" not in old  # the input is not mutated
    unversioned = dict(old)
    del unversioned["schema"]
    assert (upgrade_record(unversioned) or {}).get("schema") == 1  # no version: the original schema
    stop = upgrade_record({"record_type": "budget_stop", "schema": 1, "max_minutes": 5})
    assert stop is not None and stop["reason"] == "max_minutes"
    assert upgrade_record(record("a")) == record("a")  # current records pass through unchanged
    assert upgrade_record({**record("a"), "schema": RECORD_SCHEMA + 1}) is None


def test_report_reads_older_records_and_skips_newer_ones(tmp_path: Path) -> None:
    root = tmp_path / "results"
    write(root / "2026-11-06" / "m_1.jsonl", [schema1_record("a", model="m:1", session_id="20261106T000000Z-old")])
    write(root / "2026-11-07" / "m_2.jsonl", [record("a", model="m:2", session_id=NEW),
                                              {**record("b", model="m:2", session_id=NEW), "schema": 99}])
    records, warnings = load_records(root)
    assert any("schema newer than" in w and "m_2.jsonl" in w for w in warnings)
    assert sorted(r["case"] for r in records) == ["a", "a"]
    summary = build_summary(records)
    assert summary["record_schemas"] == {"1": 1, str(RECORD_SCHEMA): 1}
    rows = {r["model"]: r for r in summary["rows"]}
    assert rows["m:1"]["provenance"]["schema"] == 1 and rows["m:1"]["provenance"]["harness_hash"] is None
    assert rows["m:2"]["provenance"]["schema"] == RECORD_SCHEMA
    md = render_markdown(summary)
    assert "| Schema |" in md and "**Older records.**" in md


def test_resume_never_joins_a_session_of_an_older_schema(tmp_path: Path) -> None:
    prov = {k: "x" for k in runner_module.RESUME_KEYS}
    write(tmp_path / "2026-11-07" / "fake_agent.jsonl",
          [{**record("a", model="fake:agent", suite="offline", session_id=NEW), **prov, "schema": 1}])
    point = runner_module.resume_point(tmp_path, "offline", "fake:agent", prov)
    assert point.session is None and "record schema" in point.note
    write(tmp_path / "2026-11-07" / "fake_agent.jsonl",
          [{**record("a", model="fake:agent", suite="offline", session_id=NEW), **prov}])
    assert runner_module.resume_point(tmp_path, "offline", "fake:agent", prov).session == NEW


# ---------------------------------------------------------------------------------------------------
# 3. prompt_hash covers every model-visible text
# ---------------------------------------------------------------------------------------------------


def test_prompt_hash_includes_the_tools_module_and_rendered_texts() -> None:
    assert "app/graph/tools.py" in cases.MODEL_VISIBLE_FILES and "app/graph/prompts.py" in cases.MODEL_VISIBLE_FILES
    for rel in cases.MODEL_VISIBLE_FILES:
        assert (cases.BACKEND_DIR / rel).is_file(), rel
    text = json.loads(cases.model_visible_text())
    assert set(text) == {"agent_system", "parse_system", "nudge", "tools", "parse_schema"}
    assert [t["function"]["name"] for t in text["tools"]] == ["search_places", "search_events", "estimate_travel",
                                                              "check_plan", "submit_plan"]
    assert "hard_constraints" in text["parse_schema"]["properties"]  # the LLMParse structured-output schema
    assert len(cases.prompt_hash()) == 12 and cases.prompt_hash() == cases.prompt_hash()


def test_prompt_hash_changes_when_a_tool_description_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.graph.tools as tools

    before = cases.prompt_hash()
    original = tools.tool_schemas

    def edited() -> list[dict[str, Any]]:
        schemas = original()
        schemas[0]["function"]["description"] += " Edited."
        return schemas

    monkeypatch.setattr(tools, "tool_schemas", edited)
    assert cases.prompt_hash() != before


def test_prompt_hash_changes_when_a_model_visible_file_changes(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    for rel in cases.MODEL_VISIBLE_FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(cases.BACKEND_DIR / rel, tmp_path / rel)
    monkeypatch.setattr(cases, "BACKEND_DIR", tmp_path)
    before = cases.prompt_hash()
    with (tmp_path / "app/graph/tools.py").open("a", encoding="utf-8") as fh:
        fh.write("\n# a changed tool result text\n")
    assert cases.prompt_hash() != before


# ---------------------------------------------------------------------------------------------------
# 4. git_dirty: generated output ignored, compared on --resume
# ---------------------------------------------------------------------------------------------------


IGNORED = ["backend/evals/results", "docs/EVAL_REPORT.md", ".claude"]


def test_tree_dirty_ignores_changes_confined_to_generated_output() -> None:
    dirty = runner_module.tree_dirty
    assert dirty("", IGNORED) is False
    assert dirty(" M backend/evals/results/summary.json\0?? backend/evals/results/2026-11-03/\0", IGNORED) is False
    assert dirty(" M docs/EVAL_REPORT.md\0?? .claude/\0", IGNORED) is False
    assert dirty(" M backend/app/graph/prompts.py\0", IGNORED) is True
    assert dirty("?? backend/evals/\0", IGNORED) is True  # a collapsed parent may hold code: dirty
    assert dirty("?? backend/evals/results_old.py\0", IGNORED) is True  # a prefix is not a parent
    # renames: -z lists the destination, then the source; code on either side is dirty
    assert dirty("R  backend/evals/results/a.jsonl\0backend/evals/results/b.jsonl\0", IGNORED) is False
    assert dirty("R  backend/evals/results/a.py\0backend/app/a.py\0", IGNORED) is True


def git(root: Path, *cmd: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c",
                           "commit.gpgsign=false", *cmd], cwd=root, capture_output=True, text=True,
                          check=True).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_info_ignores_results_being_written(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app" / "x.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    ignore = [root / "backend" / "evals" / "results", root / "docs" / "EVAL_REPORT.md"]
    sha, dirty = runner_module.git_info(root, ignore)
    assert len(sha) == 12 and dirty is False
    results = root / "backend" / "evals" / "results" / "2026-11-03"
    results.mkdir(parents=True)
    (results / "m.jsonl").write_text("{}\n", encoding="utf-8")
    assert runner_module.git_info(root, ignore)[1] is False
    new_code = root / "backend" / "evals" / "new_check.py"  # untracked code next to the results: dirty
    new_code.write_text("y = 1\n", encoding="utf-8")
    assert runner_module.git_info(root, ignore)[1] is True
    new_code.unlink()
    assert runner_module.git_info(root, ignore)[1] is False
    (root / "backend" / "app" / "x.py").write_text("x = 2\n", encoding="utf-8")
    assert runner_module.git_info(root, ignore)[1] is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert runner_module.git_info(plain, ignore)[1] is None  # not a repository: unknown, never guessed


def test_generated_paths_cover_results_report_tooling_and_out_dir(tmp_path: Path) -> None:
    from evals import REPO_ROOT, REPORT_PATH, RESULTS_DIR

    paths = runner_module.generated_paths(tmp_path)
    assert RESULTS_DIR in paths and REPORT_PATH in paths and REPO_ROOT / ".claude" in paths and tmp_path in paths


async def test_resume_does_not_mix_dirty_and_clean_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "git_dirty" in runner_module.RESUME_KEYS
    base = ["--suite", "offline", "--cases", "negative_budget,vague_free_text", "--out", str(tmp_path)]
    monkeypatch.setattr(runner_module, "git_info", lambda root=None, ignore=None: ("abc123def456", True))
    assert await run(parse_args([*base, "--max-minutes", "0"])) == 3  # a dirty session that did nothing yet
    monkeypatch.setattr(runner_module, "git_info", lambda root=None, ignore=None: ("abc123def456", False))
    assert await run(parse_args([*base, "--resume"])) == 0  # clean: must not join the dirty session
    recs = read(only_file(tmp_path, "fake_agent.jsonl"))
    stop, runs = recs[0], [r for r in recs if r["record_type"] == "run"]
    assert stop["record_type"] == "budget_stop" and stop["git_dirty"] is True
    assert len(runs) == 2 and {r["git_dirty"] for r in runs} == {False}
    assert {r["session"] for r in runs} != {stop["session"]}  # a new session, not the dirty one


def test_resume_point_compares_the_dirty_flag(tmp_path: Path) -> None:
    prov = {k: "x" for k in runner_module.RESUME_KEYS} | {"git_dirty": False}
    write(tmp_path / "2026-11-07" / "fake_agent.jsonl",
          [{**record("a", model="fake:agent", suite="offline", session_id=NEW), **prov}])
    point = runner_module.resume_point(tmp_path, "offline", "fake:agent", {**prov, "git_dirty": True})
    assert point.session is None and "git_dirty" in point.note
    assert runner_module.resume_point(tmp_path, "offline", "fake:agent", prov).session == NEW


# ---------------------------------------------------------------------------------------------------
# 5. A harness exception writes an error record
# ---------------------------------------------------------------------------------------------------


async def test_harness_exception_writes_an_error_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    def broken(obs: Any) -> Any:
        raise RuntimeError("assertion code exploded")

    monkeypatch.setattr(runner_module, "evaluate", broken)
    args = parse_args(["--suite", "offline", "--cases", "assignment_example,negative_budget", "--out",
                       str(tmp_path / "results")])
    assert await run(args) == 2  # the harness failed
    recs = read(only_file(tmp_path / "results", "fake_agent.jsonl"))
    assert [r["case"] for r in recs] == ["assignment_example", "negative_budget"]  # nothing vanished
    for r in recs:
        assert r["record_type"] == "run" and r["status"] == "error" and r["hard_pass"] is False
        assert r["assertions"] == [] and r["error_categories"] == ["harness_exception"]
        assert r["error"].startswith("harness_exception: RuntimeError: assertion code exploded")
        assert "RuntimeError" in r["traceback"] and r["schema"] == RECORD_SCHEMA
        assert r["usage"]["llm_calls"] >= 0 and r["expected_statuses"] and r["split"] and r["category"]
    assert recs[0]["usage"]["llm_calls"] >= 1  # the planner ran before the harness broke: its usage is kept
    assert "harness_exception: RuntimeError" in capsys.readouterr().out

    row = model_metrics(recs)
    assert row["runs"] == 2 and row["hard_pass_rate"] == 0.0 and row["harness_exceptions"] == 2
    summary = build_summary(load_records(tmp_path / "results")[0])
    offline = summary["suites"]["offline"]
    assert offline["rows"][0]["hard_passes"] == 0 and len(offline["failures"]) == 2
    assert [e["case"] for e in summary["harness_exceptions"]] == ["assignment_example", "negative_budget"]
    md = render_markdown(summary)
    assert "Harness exceptions" in md and "harness_exception ×2" in md and "no assertions ran" in md


async def test_error_records_redact_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evals.assertions import SENTINEL_SETTINGS

    secret = sorted(SENTINEL_SETTINGS.values())[0]

    def leaky(obs: Any) -> Any:
        raise RuntimeError(f"bad key {secret}")

    monkeypatch.setattr(runner_module, "evaluate", leaky)
    args = parse_args(["--suite", "offline", "--cases", "negative_budget", "--out", str(tmp_path)])
    assert await run(args) == 2
    rec = read(only_file(tmp_path, "fake_agent.jsonl"))[0]
    assert secret not in json.dumps(rec) and "[redacted]" in rec["error"]
    assert str(Path.home()) not in rec["traceback"] and "backend/evals/runner.py" in rec["traceback"]


def test_error_status_is_a_hard_failure_even_without_an_error_text() -> None:
    rec = {**record("a"), "status": "error", "assertions": [], "error": None}
    assert record_hard_pass(rec) is False
    row = model_metrics([rec, record("b")])
    assert row["hard_passes"] == 1 and row["hard_pass_rate"] == 0.5


# ---------------------------------------------------------------------------------------------------
# 6. backend/.env reaches the process environment at runner start
# ---------------------------------------------------------------------------------------------------


def clear_env(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "placeholder")  # registers the original value, so the test restores it
    monkeypatch.delenv(name)


def test_load_env_exports_keys_only_present_in_backend_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_env(monkeypatch, "ANTHROPIC_API_KEY")
    clear_env(monkeypatch, "XAI_API_KEY")
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\nXAI_API_KEY=xai-from-dotenv\n", encoding="utf-8")
    assert runner_module.unavailable_reason("anthropic:claude-opus-5-5", {}) == "ANTHROPIC_API_KEY is not set"
    monkeypatch.setenv("XAI_API_KEY", "xai-from-shell")  # the shell's value wins
    settings = runner_module.load_env(env)
    assert settings is not None
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"
    assert os.environ["XAI_API_KEY"] == "xai-from-shell"
    # the live suite passes no keys in extra: availability now sees the key from .env
    assert runner_module.unavailable_reason("anthropic:claude-opus-5-5", {}) is None


def test_main_loads_backend_env_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_main(monkeypatch)
    calls: list[Path | None] = []
    monkeypatch.setattr(runner_module, "load_env", lambda env_file=None: calls.append(env_file))
    assert runner_module.main(["--suite", "live"]) == 0
    assert calls == [None]


def test_load_env_without_a_file_is_harmless(tmp_path: Path) -> None:
    assert runner_module.load_env(tmp_path / "missing.env") is not None


# ---------------------------------------------------------------------------------------------------
# 7. Cost per plan and per run
# ---------------------------------------------------------------------------------------------------


def test_cost_per_plan_divides_by_runs_that_produced_a_plan() -> None:
    recs = [
        record("a", status="complete", cost=0.03),
        record("b", status="needs_clarification", mode=None, expected=("needs_clarification",), cost=0.01),
        record("c", status="fallback", mode="deterministic", cost=0.02),
        record("d", status="no_feasible_plan", mode=None, expected=("no_feasible_plan",), cost=0.0),
    ]
    row = model_metrics(recs)
    assert row["n"]["plans"] == 2 and row["total_cost_usd"] == 0.06
    assert row["cost_per_plan_usd"] == 0.03 and row["cost_per_run_usd"] == 0.015
    no_plans = model_metrics([record("b", status="needs_clarification", mode=None, cost=0.01)])
    assert no_plans["cost_per_plan_usd"] is None and no_plans["cost_per_run_usd"] == 0.01


def test_report_shows_cost_per_plan_and_per_run_with_their_n(tmp_path: Path) -> None:
    root = tmp_path / "results"
    write(root / "2026-11-07" / "m_1.jsonl", [
        record("a", status="complete", cost=0.30, session_id=NEW),
        record("b", status="needs_clarification", mode=None, cost=0.10, session_id=NEW),
    ])
    md = render_markdown(build_summary(load_records(root)[0]))
    assert "| Cost/plan | Cost/run |" in md
    assert "$0.40 (n=1) | $0.20 (n=2) |" in md
    assert "**Cost.**" in md and "runs that produced a plan" in md


# ---------------------------------------------------------------------------------------------------
# 8. --max-cost-usd
# ---------------------------------------------------------------------------------------------------


def priced(monkeypatch: pytest.MonkeyPatch, cost: float) -> None:
    """Every run record costs ``cost`` (the fake model is free; the budget logic only reads usage.cost_usd)."""
    original = runner_module.build_record

    def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
        rec = original(*args, **kwargs)
        rec["usage"] = {**rec["usage"], "cost_usd": cost}
        return rec

    monkeypatch.setattr(runner_module, "build_record", build)


def test_max_cost_usd_is_validated() -> None:
    assert parse_args([]).max_cost_usd is None
    assert parse_args(["--max-cost-usd", "2.5"]).max_cost_usd == 2.5
    with pytest.raises(SystemExit):
        parse_args(["--max-cost-usd", "-1"])
    with pytest.raises(SystemExit):
        parse_args(["--max-cost-usd", "nan"])


def test_record_cost_includes_the_judge() -> None:
    rec = {**record("a", cost=0.25), "judge": {"status": "scored", "usage": {"cost_usd": 0.05}}}
    assert runner_module.record_cost_usd(rec) == pytest.approx(0.30)
    assert runner_module.record_cost_usd({"usage": None, "judge": None}) == 0.0


async def test_max_cost_usd_stops_every_model_and_resume_finishes(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    priced(monkeypatch, 0.5)
    base = ["--suite", "offline", "--models", "fake:agent,baseline:deterministic", "--cases",
            "negative_budget,vague_free_text,missing_city_api", "--out", str(tmp_path), "--no-gate"]
    # 0 -> run (0.5) -> run (1.0) -> 1.0 > 0.75: stop; the next model has no budget left at all
    assert await run(parse_args([*base, "--max-cost-usd", "0.75"])) == 3
    fake = read(only_file(tmp_path, "fake_agent.jsonl"))
    assert [r["record_type"] for r in fake] == ["run", "run", "budget_stop"]
    stop = fake[-1]
    assert stop["reason"] == "max_cost_usd" and stop["max_cost_usd"] == 0.75 and stop["spent_usd"] == 1.0
    assert stop["remaining"] == 1 and stop["session"] == fake[0]["session"]
    base_recs = read(only_file(tmp_path, "baseline_deterministic.jsonl"))
    assert [r["record_type"] for r in base_recs] == ["budget_stop"] and base_recs[0]["remaining"] == 3
    out = capsys.readouterr().out
    assert "--max-cost-usd 0.75 exceeded" in out and "estimated cost this invocation: $1.0000 of a $0.75" in out

    summary = build_summary(load_records(tmp_path)[0])
    stops = summary["stopped_early"]
    assert [(s["model"], s["reason"], s["remaining"]) for s in stops] == [("fake:agent", "max_cost_usd", 1)]
    assert "the $0.75 cost budget ($1.00 spent)" in render_markdown(summary)

    assert await run(parse_args([*base, "--resume"])) == 0  # no budget: both models finish their sessions
    fake = read(only_file(tmp_path, "fake_agent.jsonl"))
    runs = [r for r in fake if r["record_type"] == "run"]
    assert sorted(r["case"] for r in runs) == ["missing_city_api", "negative_budget", "vague_free_text"]
    assert len({r["session"] for r in runs}) == 1


async def test_cost_budget_is_not_hit_by_free_runs(tmp_path: Path) -> None:
    args = parse_args(["--suite", "offline", "--cases", "negative_budget,vague_free_text", "--out", str(tmp_path),
                       "--max-cost-usd", "0"])
    assert await run(args) == 0  # $0 spent never exceeds a $0 budget
    assert [r["record_type"] for r in read(only_file(tmp_path, "fake_agent.jsonl"))] == ["run", "run"]
