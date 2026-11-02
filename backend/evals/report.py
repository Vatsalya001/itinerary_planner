"""Eval report: ``python -m evals.report [--results DIR]``.

Reads every ``<results>/<YYYY-MM-DD>/*.jsonl`` file, keeps one session per (suite, model) (the latest that covers
every case of its suite, else the latest), and writes
``<results>/summary.json`` (the /evals page contract: ``{generated_at, git_sha, rows, categories}``, extended only
additively) and ``docs/EVAL_REPORT.md``. Every number is computed from the JSONL records; nothing is typed by hand.
``--publish`` also stores the summary through app.store (``DATABASE_URL``) so a deployed /evals page can serve it.

Older records: every record is read through ``evals.metrics.upgrade_record``, which accepts every schema up to
``evals.RECORD_SCHEMA`` (fields a schema did not record read as "not recorded"); records from a newer schema are
skipped with a warning. ``summary.record_schemas`` counts the versions read and the provenance table shows each row's
schema.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals import DATE_DIR, RECORD_SCHEMA, REPORT_PATH, RESULTS_DIR
from evals.cases import load_cases, load_spec
from evals.metrics import (
    HARNESS_EXCEPTION,
    by_category,
    by_split,
    failures,
    model_metrics,
    record_schema,
    run_records,
    upgrade_record,
)

KIND_ORDER = {"baseline": 0, "api": 1, "local": 2, "fake": 3}
FAILURE_EXAMPLES_PER_MODEL = 40


# ---------------------------------------------------------------------------------------------------
# Loading and selection
# ---------------------------------------------------------------------------------------------------


def load_records(results_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """All records from dated subdirectories (scratch dirs such as tmp/ are ignored), upgraded to the current schema
    (``upgrade_record``), plus warnings. Records from a newer schema than this report understands are skipped."""
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    newer: dict[str, int] = defaultdict(int)
    if not results_dir.is_dir():
        return records, [f"results directory {results_dir} does not exist"]
    for day in sorted(p for p in results_dir.iterdir() if p.is_dir() and DATE_DIR.match(p.name)):
        for path in sorted(day.glob("*.jsonl")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    warnings.append(f"{path.name}:{lineno}: not JSON, skipped")
                    continue
                if not isinstance(rec, dict):
                    warnings.append(f"{path.name}:{lineno}: not a record object, skipped")
                    continue
                upgraded = upgrade_record(rec)
                if upgraded is None:
                    newer[f"{day.name}/{path.name}"] += 1
                    continue
                upgraded["_file"] = f"{day.name}/{path.name}"
                records.append(upgraded)
    for name, count in sorted(newer.items()):
        warnings.append(f"{name}: {count} record(s) use a schema newer than {RECORD_SCHEMA}, skipped (update the "
                        "report code)")
    return records, warnings


def select_latest(records: list[dict[str, Any]], suite_cases: dict[str, int] | None = None,
                  ) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    """(run records of the session shown per (suite, model), skip records newer than those runs).

    The session shown is the latest *complete* one (it covers every case of its suite in ``suite_cases``); when no
    session is complete, the latest one. So an ad-hoc partial run, or a long run still in progress, never hides the
    last full result."""
    cases: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in run_records(records):
        cases[(str(r.get("suite")), str(r.get("model")))][str(r.get("session"))].add(str(r.get("case")))
    chosen: dict[tuple[str, str], str] = {}
    for key, by_session in cases.items():
        need = (suite_cases or {}).get(key[0])
        complete = [sess for sess, cs in by_session.items() if need and len(cs) >= need]
        chosen[key] = max(complete) if complete else max(by_session)
    runs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in run_records(records):
        key = (str(r.get("suite")), str(r.get("model")))
        if str(r.get("session")) == chosen[key]:
            runs[key].append(r)
    skips: dict[tuple[str, str], dict[str, Any]] = {}
    for r in records:
        if r.get("record_type") != "skip":
            continue
        key = (str(r.get("suite")), str(r.get("model")))
        if key in chosen and str(r.get("session")) <= chosen[key]:
            continue  # superseded by a later run of the same model
        if key not in skips or str(r.get("session")) > str(skips[key].get("session")):
            skips[key] = r
    return dict(runs), sorted(skips.values(), key=lambda r: (str(r.get("suite")), str(r.get("model"))))


# ---------------------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------------------


def _suite_case_counts() -> dict[str, int]:
    try:
        cases = load_cases(load_spec())
    except (OSError, ValueError):
        return {}
    counts: dict[str, int] = defaultdict(int)
    for c in cases:
        for s in c.suites:
            counts[s] += 1
    return dict(counts)


def _row(recs: list[dict[str, Any]], suite_cases: int | None) -> dict[str, Any]:
    row = model_metrics(recs)
    first = recs[0]
    row["splits"] = by_split(recs)
    row["categories"] = by_category(recs)
    row["session"] = first.get("session")
    row["suite"] = first.get("suite")
    row["coverage"] = {"cases": row["cases"], "suite_cases": suite_cases,
                       "partial": bool(suite_cases and row["cases"] < suite_cases)}
    row["provenance"] = {k: first.get(k) for k in ("schema", "git_sha", "git_dirty", "prompt_hash", "registry_hash",
                                                    "fixture_hash", "eval_cases_hash", "harness_hash")}
    stamps = sorted(str(r.get("timestamp")) for r in recs if r.get("timestamp"))
    row["started_at"], row["finished_at"] = (stamps[0], stamps[-1]) if stamps else (None, None)
    row["files"] = sorted({str(r.get("_file")) for r in recs if r.get("_file")})
    return row


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (KIND_ORDER.get(str(r.get("kind")), 9), -(r.get("hard_pass_rate") or 0),
                                       str(r.get("model"))))


def _suite_block(runs: dict[tuple[str, str], list[dict[str, Any]]], suite: str,
                 counts: dict[str, int]) -> dict[str, Any]:
    rows = _sort_rows([_row(recs, counts.get(suite)) for (s, _), recs in runs.items() if s == suite])
    categories: dict[str, dict[str, Any]] = defaultdict(dict)
    categories_n: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in rows:
        for cat, stats in row["categories"].items():
            categories[cat][str(row["model"])] = stats["hard_pass_rate"]
            categories_n[cat][str(row["model"])] = stats["runs"]
    fails: list[dict[str, Any]] = []
    for (s, _), recs in sorted(runs.items()):
        if s == suite:
            fails += failures(recs)
    return {"rows": rows, "categories": dict(sorted(categories.items())),
            "categories_n": dict(sorted(categories_n.items())), "failures": fails}


def superseded_sessions(records: list[dict[str, Any]],
                        runs: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Older sessions kept in the files for audit (never deleted, never counted)."""
    used = {(k[0], k[1]): str(v[0].get("session")) for k, v in runs.items() if v}
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for r in run_records(records):
        key = (str(r.get("suite")), str(r.get("model")), str(r.get("session")))
        if used.get(key[:2]) != key[2]:
            counts[key] += 1
    return [{"suite": s, "model": m, "session": sess, "runs": n,
             "relation": "newer, incomplete" if sess > used.get((s, m), "") else "older"}
            for (s, m, sess), n in sorted(counts.items())]


def mark_prompt_versions(rows: list[dict[str, Any]], current: str | None) -> None:
    """Flag rows measured with different agent prompts/tools than the current code (the baseline uses none)."""
    for row in rows:
        recorded = (row.get("provenance") or {}).get("prompt_hash")
        row["prompt_current"] = row.get("kind") == "baseline" or current is None or recorded == current


def build_summary(records: list[dict[str, Any]], generated_at: str | None = None) -> dict[str, Any]:
    from evals.cases import prompt_hash

    counts = _suite_case_counts()
    runs, skips = select_latest(records, counts)
    suites = {s: _suite_block(runs, s, counts) for s in ("models", "offline", "live")}
    models_block = suites["models"]
    all_rows = [r for b in suites.values() for r in b["rows"]]
    try:
        current_prompt = prompt_hash()
    except Exception:  # the report must still render if the app can't be imported
        current_prompt = None
    mark_prompt_versions(all_rows, current_prompt)
    newest = max(all_rows, key=lambda r: str(r.get("finished_at") or ""), default=None)
    judge_models = sorted({m for r in all_rows for m in r.get("judge_models") or []})
    rubrics = sorted({v for r in all_rows for v in r.get("judge_rubric_versions") or []})
    judge_requested = sorted({str((r.get("judge") or {}).get("model")) for r in run_records(records)
                              if isinstance(r.get("judge"), dict) and r["judge"].get("status") == "unavailable"})
    return {
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": (newest or {}).get("provenance", {}).get("git_sha"),
        "rows": models_block["rows"],
        "categories": models_block["categories"],
        # additive fields (the /evals page renders rows and categories; everything else is for the report)
        "categories_n": models_block["categories_n"],
        "suites": {s: b for s, b in suites.items() if s != "models" and b["rows"]},
        "failures": models_block["failures"],
        "skipped": [{"suite": r.get("suite"), "model": r.get("model"), "kind": r.get("kind"),
                     "reason": r.get("reason"), "timestamp": r.get("timestamp")} for r in skips],
        "judge": {"models": judge_models, "rubric_versions": rubrics, "unavailable": judge_requested}
        if judge_models or judge_requested else None,
        "record_count": len(records),
        "superseded_sessions": superseded_sessions(records, runs),
        "not_run": not_run(models_block["rows"], skips),
        "stopped_early": stopped_early(records, all_rows),
        "harness_exceptions": harness_exceptions(runs),
        "record_schemas": record_schemas(records),
        "current_prompt_hash": current_prompt,
    }


def harness_exceptions(runs: dict[tuple[str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Runs in the sessions shown that the harness could not finish (counted as hard failures in every rate)."""
    out = [{"suite": r.get("suite"), "model": r.get("model"), "session": r.get("session"), "case": r.get("case"),
            "repeat": r.get("repeat"), "error": r.get("error"), "timestamp": r.get("timestamp")}
           for recs in runs.values() for r in recs
           if r.get("status") == "error" or HARNESS_EXCEPTION in (r.get("error_categories") or [])]
    return sorted(out, key=lambda e: (str(e["suite"]), str(e["model"]), str(e["case"]), str(e["repeat"])))


def record_schemas(records: list[dict[str, Any]]) -> dict[str, int]:
    """How many records of each schema version were read (older versions are upgraded on load)."""
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        counts[str(record_schema(r))] += 1
    return dict(sorted(counts.items()))


def stopped_early(records: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Budget stops (--max-minutes or --max-cost-usd) in the sessions shown, while those sessions are incomplete."""
    partial = {(str(r.get("suite")), str(r.get("model")), str(r.get("session"))): r for r in rows
               if (r.get("coverage") or {}).get("partial")}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        if rec.get("record_type") != "budget_stop":
            continue
        key = (str(rec.get("suite")), str(rec.get("model")), str(rec.get("session")))
        if key in partial and str(rec.get("timestamp")) >= str(latest.get(key[:2], {}).get("timestamp", "")):
            latest[key[:2]] = rec
    return [{"suite": r.get("suite"), "model": r.get("model"), "session": r.get("session"),
             "reason": r.get("reason") or "max_minutes", "max_minutes": r.get("max_minutes"),
             "max_cost_usd": r.get("max_cost_usd"), "spent_usd": r.get("spent_usd"), "remaining": r.get("remaining"),
             "timestamp": r.get("timestamp")}
            for _, r in sorted(latest.items())]


def not_run(rows: list[dict[str, Any]], skips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model-matrix rows (eval_cases.json) with neither results nor a skip record in the models suite."""
    try:
        matrix = [str(m["id"]) for m in load_spec().get("model_matrix") or []]
    except (OSError, ValueError, KeyError):
        return []
    seen = {str(r.get("model")) for r in rows} | {str(s.get("model")) for s in skips if s.get("suite") == "models"}
    out = []
    for model_id in matrix:
        if model_id in seen:
            continue
        reason = "no results yet"
        try:
            from app.llm.registry import PendingModelError, RegistryError, get_spec

            get_spec(model_id)
        except PendingModelError:
            reason = "pending registry ID (no model pinned yet)"
        except RegistryError as exc:
            reason = f"not runnable: {exc}"
        out.append({"model": model_id, "reason": reason})
    return out


# ---------------------------------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------------------------------


def _pct(num: Any, den: Any) -> str:
    if not isinstance(den, int) or den == 0 or num is None:
        return "—"
    return f"{100 * num / den:.1f}% ({num}/{den})"


def _num(value: Any, digits: int = 2, n: Any = None) -> str:
    if value is None:
        return "—"
    text = f"{value:.{digits}f}" if isinstance(value, float) else str(value)
    return f"{text} (n={n})" if n is not None else text


def _ms(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{value} ms"


def _cost(value: Any, n: Any = None) -> str:
    if value is None:
        return "—"
    text = f"${value:.4f}" if 0 < value < 0.01 else f"${value:.2f}"
    return f"{text} (n={n})" if n is not None else text


def _esc(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(_esc(c) for c in row) + " |" for row in rows]
    return out


def leaderboard_lines(rows: list[dict[str, Any]]) -> list[str]:
    header = ["Model", "Kind", "Cases", "Hard pass", "Status acc.", "Agent success", "1st submit valid",
              "Repairs/plan", "Tool errors/run", "Clarif. precision", "Clarif. recall", "Parse acc.",
              "Security", "Judge (1-5)", "p50", "p95", "Cost/plan", "Cost/run"]
    body = []
    for r in rows:
        n = r.get("n") or {}
        cov = r.get("coverage") or {}
        cases = f"{cov.get('cases')}/{cov.get('suite_cases')}" if cov.get("suite_cases") else str(r.get("cases"))
        if cov.get("partial"):
            cases += " (partial)"
        stale = "" if r.get("prompt_current", True) else " †"
        body.append([
            f"`{r['model']}`{stale}", str(r.get("kind")), cases,
            _pct(r.get("hard_passes"), r.get("runs")),
            _pct(r.get("status_ok_runs"), r.get("runs")),
            _pct(r.get("agent_successes"), n.get("feasible_runs")) if r.get("agent_success") is not None else "n/a",
            _pct(r.get("first_submit_valid"), n.get("submissions")),
            _num(r.get("repairs_per_plan"), 2, n.get("agent_plans")) if r.get("repairs_per_plan") is not None else "—",
            _num(r.get("tool_protocol_errors"), 2, r.get("runs")) if r.get("tool_protocol_errors") is not None else "n/a",
            _pct(r.get("clarification_hits"), n.get("clarifications_asked")),
            _pct(r.get("blocking_asked"), n.get("blocking_instances")),
            _pct(r.get("parse_matches"), n.get("parse_fields")),
            _pct(r.get("security_passes"), n.get("security_runs")),
            _num(r.get("judge_quality"), 2, n.get("judged_runs")) if r.get("judge_quality") is not None else "—",
            _ms(r.get("latency_p50_ms")), _ms(r.get("latency_p95_ms")),
            _cost(r.get("cost_per_plan_usd"), n.get("plans")), _cost(r.get("cost_per_run_usd"), r.get("runs")),
        ])
    return _table(header, body)


def split_lines(rows: list[dict[str, Any]]) -> list[str]:
    header = ["Model", "Dev hard pass", "Dev status acc.", "Holdout hard pass", "Holdout status acc."]
    body = []
    for r in rows:
        dev, hold = (r.get("splits") or {}).get("dev") or {}, (r.get("splits") or {}).get("holdout") or {}
        body.append([f"`{r['model']}`", _pct(dev.get("hard_passes"), dev.get("runs")),
                     _pct(dev.get("status_ok_runs"), dev.get("runs")), _pct(hold.get("hard_passes"), hold.get("runs")),
                     _pct(hold.get("status_ok_runs"), hold.get("runs"))])
    return _table(header, body)


def category_lines(rows: list[dict[str, Any]]) -> list[str]:
    cats = sorted({c for r in rows for c in (r.get("categories") or {})})
    header = ["Category", *[f"`{r['model']}`" for r in rows]]
    body = []
    for cat in cats:
        cells = []
        for r in rows:
            stats = (r.get("categories") or {}).get(cat)
            cells.append(_pct(stats["hard_passes"], stats["runs"]) if stats else "—")
        body.append([cat, *cells])
    return _table(header, body)


def pass_k_lines(rows: list[dict[str, Any]]) -> list[str]:
    rows_k = [r for r in rows if (r.get("k") or 0) > 1]
    if not rows_k:
        return ["Every case ran once (repeats = 1), so pass^k is not reported. Use `--repeats 3` for the report."]
    body = [[f"`{r['model']}`", str(r["k"]), _pct(r.get("pass_hat_k_passes"), (r.get("n") or {}).get("pass_hat_k_cases")),
             _pct(r.get("hard_passes"), r.get("runs"))] for r in rows_k]
    return _table(["Model", "k", "pass^k (cases where all k runs passed)", "Mean hard pass (runs)"], body)


def error_lines(rows: list[dict[str, Any]]) -> list[str]:
    body = []
    for r in rows:
        cats = r.get("error_categories") or {}
        text = ", ".join(f"{k} ×{v}" for k, v in cats.items()) or "none"
        body.append([f"`{r['model']}`", str(r.get("runs")), text])
    return _table(["Model", "Runs", "Error and degradation categories (runs affected)"], body)


def node_lines(rows: list[dict[str, Any]]) -> list[str]:
    nodes = sorted({n for r in rows for n in (r.get("node_latency_p50_ms") or {})})
    if not nodes:
        return ["No per-node timings recorded."]
    body = [[node, *[_ms((r.get("node_latency_p50_ms") or {}).get(node)) for r in rows]] for node in nodes]
    return _table(["Node (p50 per run)", *[f"`{r['model']}`" for r in rows]], body)


def failure_lines(fails: list[dict[str, Any]], per_model: int = FAILURE_EXAMPLES_PER_MODEL) -> list[str]:
    if not fails:
        return ["No failed runs.", ""]
    out: list[str] = []
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in fails:
        by_model[str(f.get("model"))].append(f)
    for model, items in by_model.items():
        out.append(f"**`{model}`**: {len(items)} failed run(s)" + (
            f"; first {per_model} shown" if len(items) > per_model else ""))
        out.append("")
        for f in items[:per_model]:
            head = (f"- `{f['case']}` #{f['repeat']} ({f.get('split')}, {f.get('category')}): status "
                    f"`{f.get('status')}`/{f.get('mode') or '-'}, expected {f.get('expected_statuses')}")
            out.append(head)
            if f.get("error"):
                out.append(f"  - harness: {_esc(f['error'])[:300]}")
            if f.get("status") == "error":
                out.append("  - no assertions ran (harness exception); counted as a hard failure")
            for a in f.get("failed") or []:
                out.append(f"  - `{a.get('name')}`: {_esc(a.get('detail') or '')[:300]}")
        out.append("")
    return out


def _budget_text(stop: dict[str, Any]) -> str:
    if stop.get("reason") == "max_cost_usd" and stop.get("max_cost_usd") is not None:
        spent = f" ({_cost(stop['spent_usd'])} spent)" if stop.get("spent_usd") is not None else ""
        return f"the {_cost(stop['max_cost_usd'])} cost budget{spent}"
    minutes = stop.get("max_minutes")
    return f"a {minutes:g}-minute budget" if isinstance(minutes, int | float) else "a budget stop"


def provenance_lines(rows: list[dict[str, Any]]) -> list[str]:
    body = []
    for r in rows:
        p = r.get("provenance") or {}
        dirty = " (dirty tree)" if p.get("git_dirty") else ""
        body.append([f"`{r['model']}`", str(r.get("suite")), str(r.get("session")), str(p.get("schema") or 1),
                     f"{p.get('git_sha')}{dirty}",
                     str(p.get("prompt_hash")), str(p.get("registry_hash")), str(p.get("fixture_hash")),
                     str(p.get("eval_cases_hash")), str(p.get("harness_hash") or "—"), ", ".join(r.get("files") or [])])
    return _table(["Model", "Suite", "Session", "Schema", "Git SHA", "Prompt hash", "Registry hash", "Fixture hash",
                   "eval_cases hash", "Harness hash", "Results file"], body)


def render_markdown(summary: dict[str, Any]) -> str:
    rows = summary.get("rows") or []
    suites = summary.get("suites") or {}
    judge = summary.get("judge")
    lines = [
        "# Eval report",
        "",
        f"<!-- Generated by `python -m evals.report` at {summary.get('generated_at')}. Do not edit by hand: every number "
        "below is computed from the JSONL files in backend/evals/results/. -->",
        "",
        f"Generated {summary.get('generated_at')} from {summary.get('record_count')} records (per suite and model: "
        "the latest session covering every case, else the latest session). Spec: `eval_cases.json`; method: "
        "IMPLEMENTATION_PLAN.md section 12.",
        "",
        "## Model leaderboard (`models` suite, frozen world)",
        "",
    ]
    if rows:
        lines += leaderboard_lines(rows)
        stale = [r for r in rows if not r.get("prompt_current", True)]
        if stale:
            lines += ["", f"† Measured with an earlier version of the agent's prompts and tools (prompt hash "
                      f"{', '.join(sorted({str((r.get('provenance') or {}).get('prompt_hash')) for r in stale}))} vs current "
                      f"`{summary.get('current_prompt_hash')}`), before the batched-search redesign. Not a like-for-like "
                      "comparison with current rows; rerun those models to compare."]
        lines += ["", "Rates show `(passed/n)`; `n/a` means the metric does not apply to the row (the baseline has no "
                  "agent); `—` means nothing was measured (n = 0). Cost/plan is the planner's registry-priced cost "
                  "of all runs divided by the runs that produced a plan (complete or fallback; n = plans); Cost/run "
                  "divides the same total by every run. Local and baseline rows cost $0, so compare them on wall "
                  "time. Baseline, API and local rows are separate populations: local rows run with a longer "
                  "deadline and are not production candidates.", ""]
        lines += ["### Dev vs holdout", "", *split_lines(rows), ""]
        lines += ["### Repeats", "", *pass_k_lines(rows), ""]
        lines += ["### By category (hard pass)", "", *category_lines(rows), ""]
        lines += ["### Errors and degradations", "", "Runs are never rerun or dropped; every error category is counted here.", "", *error_lines(rows), ""]
        lines += ["### Latency per node", "", *node_lines(rows), ""]
        lines += ["### Failure examples", "", *failure_lines(summary.get("failures") or [])]
    else:
        lines += ["No `models` suite results yet. Run `make eval` (or `python -m evals.runner --suite models`).", ""]

    lines += ["## Skipped models", ""]
    skipped = summary.get("skipped") or []
    if skipped:
        lines += _table(["Model", "Suite", "Reason", "When"], [[f"`{s['model']}`", str(s.get("suite")),
                                                              str(s.get("reason")), str(s.get("timestamp"))]
                                                             for s in skipped])
        lines += ["", "Skipped models are not counted as failures.", ""]
    else:
        lines += ["None.", ""]
    stops = summary.get("stopped_early") or []
    if stops:
        lines += ["Stopped early by `--max-minutes` or `--max-cost-usd` (rows above are partial; finish with "
                  "`--resume`):", ""]
        lines += [f"- `{st['model']}` ({st['suite']}, session {st['session']}): {st['remaining']} run(s) left after "
                  f"{_budget_text(st)}" for st in stops] + [""]
    exceptions = summary.get("harness_exceptions") or []
    if exceptions:
        lines += ["Harness exceptions (the harness could not finish these runs; each is recorded with status "
                  "`error` and no assertions, and counts as a hard failure):", ""]
        lines += [f"- `{e['model']}` ({e['suite']}) `{e['case']}` #{e['repeat']}: {_esc(e.get('error') or '')[:300]}"
                  for e in exceptions] + [""]
    missing = summary.get("not_run") or []
    if missing:
        lines += ["Model-matrix rows with no `models` results in this report:", ""]
        lines += [f"- `{m['model']}`: {m['reason']}" for m in missing] + [""]

    offline = suites.get("offline")
    lines += ["## Offline suite (scripted fake model)", "",
              "The offline suite drives the real graph, tools, validator, API and storage with a scripted LangChain "
              "fake model and injected faults. It tests plumbing and failure handling, **not model quality**; do "
              "not compare it with the leaderboard above.", ""]
    if offline:
        lines += [*leaderboard_lines(offline["rows"]), ""]
        lines += ["### Offline by category", "", *category_lines(offline["rows"]), ""]
        lines += ["### Offline errors and degradations (mostly injected faults)", "", *error_lines(offline["rows"]), ""]
        lines += ["### Offline failures", "", *failure_lines(offline.get("failures") or [])]
    else:
        lines += ["No offline results yet (`make eval-offline`).", ""]

    live = suites.get("live")
    if live:
        lines += ["## Live suite (real providers; reported separately)", "", *leaderboard_lines(live["rows"]), ""]
        lines += failure_lines(live.get("failures") or [])

    lines += ["## Methodology and caveats", ""]
    lines += [
        "- **Frozen synthetic world.** The models and offline suites use fixture places (synthetic attributes on "
        "real or OSM-derived names), fixed weather, events, travel and a frozen clock (Thursday 2026-11-05 10:00 "
        "IST, target Saturday 2026-11-07). Every model sees identical tool results, so rows are comparable, but "
        "scores describe this small world, not live data quality.",
        "- **Independent checks.** Hard assertions recompute cost, time, opening hours, evidence, pacing, daylight, "
        "Plan B, prose and grounding from the raw fixture files and from the request text, never from the planner's "
        "own validation flags. Every stop must have been returned by a provider during the same run (recorded by "
        "proxies around the providers). Case-specific assertions are code where machine-checkable; a few use "
        "keyword proxies for \"the text mentions X\" (marked in their detail). Assertions that need a browser or "
        "judgement are marked `judge` and never count toward the hard pass rate.",
        "- **Hard pass** requires every applicable code assertion, including the status being in the case's "
        "expected set: a deterministic fallback on a feasible case is a failure for a model row. A run the harness "
        "could not finish (harness exception) is recorded with status `error` and no assertions, and counts as a "
        "hard failure in every rate (error category `harness_exception`).",
        "- **Cost.** Registry-priced tokens of the planner's model calls (judge calls excluded). Cost/plan divides "
        "the total over all runs by the runs that produced a plan (complete or fallback), so clarifications, "
        "rejections and failed runs are charged to the plans delivered; Cost/run divides it by every run.",
        "- **Clarification precision/recall** count one instance per planner call; the first call of a case that "
        "supplies `clarification_answers` counts as blocking (the spec provides answers because a question is "
        "expected), its resume call as non-blocking.",
        "- **Separation.** Baseline (no LLM), hosted API and local (Ollama) rows are separate populations. Local rows "
        "get a 240 s run deadline instead of 60 s; their latency is reported, not hidden. Holdout cases are reported "
        "separately and must never be tuned against.",
        "- **Sample size.** n is shown for every number. With one repeat per case, a single flaky run moves a rate "
        "by several points; use three repeats (pass^3) before drawing conclusions.",
    ]
    if judge and judge.get("models"):
        lines.append(f"- **Judge.** Model(s) {', '.join('`' + m + '`' for m in judge['models'])}, rubric "
                     f"{', '.join(judge.get('rubric_versions') or [])}. Hard failures score 1 on every axis. The judge "
                     "may share a provider with a candidate; re-judge a sample with a second provider before trusting "
                     "small differences.")
    else:
        extra = f" (requested but unavailable: {', '.join(judge['unavailable'])})" if judge and judge.get(
            "unavailable") else ""
        lines.append(f"- **Judge.** No judge model ran{extra}, so judge quality is null (shown as —).")
    older = [v for v in summary.get("record_schemas") or {} if v != str(RECORD_SCHEMA)]
    if older:
        lines.append(f"- **Older records.** Some records use schema {', '.join(older)} (current: {RECORD_SCHEMA}; "
                     "see the Schema column in the provenance table). Fields they did not record "
                     "(harness hash, parsed preferences, message) read as missing; their dirty-tree flag also counted "
                     "results being written and their prompt hash covered app/graph/prompts.py only, so neither is "
                     "comparable with newer rows.")
    lines += ["", "## Provenance", ""]
    prov_rows = rows + [r for b in suites.values() for r in b.get("rows") or []]
    lines += provenance_lines(prov_rows) if prov_rows else ["No results."]
    old = summary.get("superseded_sessions") or []
    if old:
        lines += ["", "Other sessions are kept in the same files for audit and are not counted above. The report shows "
                  "the latest session that covers every case of its suite (else the latest one), so older sessions "
                  "and newer incomplete ones (ad-hoc subsets or runs still in progress) are listed here:", ""]
        lines += _table(["Suite", "Model", "Session", "Runs", "Relation"],
                        [[str(o["suite"]), f"`{o['model']}`", str(o["session"]), str(o["runs"]),
                          str(o.get("relation"))] for o in old])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------


async def publish(summary: dict[str, Any]) -> None:
    from app.config import get_settings
    from app.store.factory import create_store

    store = create_store(get_settings().database_url)
    await store.init()
    try:
        await store.save_eval_summary(summary)
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m evals.report", description=__doc__.split("\n\n")[0])
    p.add_argument("--results", default=str(RESULTS_DIR), help="results directory (default: evals/results)")
    p.add_argument("--summary", default="", help="summary.json path (default: <results>/summary.json)")
    p.add_argument("--report", default=str(REPORT_PATH), help="markdown report path (default: docs/EVAL_REPORT.md)")
    p.add_argument("--publish", action="store_true", help="also save the summary to the app store (DATABASE_URL)")
    args = p.parse_args(argv)
    results = Path(args.results)
    records, warnings = load_records(results)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    summary = build_summary(records)
    summary_path = Path(args.summary) if args.summary else results / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(summary), encoding="utf-8")
    print(f"{len(records)} records -> {summary_path} and {report_path}")
    for row in summary["rows"]:
        print(f"  models  {row['model']:<26} hard pass {row['hard_passes']}/{row['runs']}")
    for suite, block in (summary.get("suites") or {}).items():
        for row in block["rows"]:
            print(f"  {suite:<7} {row['model']:<26} hard pass {row['hard_passes']}/{row['runs']}")
    for s in summary["skipped"]:
        print(f"  skipped {s['model']} ({s['suite']}): {s['reason']}")
    if args.publish:
        asyncio.run(publish(summary))
        print("published the summary to the app store")
    return 0


if __name__ == "__main__":
    sys.exit(main())
