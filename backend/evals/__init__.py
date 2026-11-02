"""Eval harness for the Perfect Saturday Planner (IMPLEMENTATION_PLAN.md section 12, spec in eval_cases.json).

* ``evals.cases``      loads eval_cases.json, applies its merge semantics and builds per-case settings and deps.
* ``evals.assertions`` independent checks: recomputes cost, time, hours, evidence, pacing and prose from the raw
  fixture files (never from the planner's own validation flags), plus case-specific assertions.
* ``evals.metrics``    per-model metrics exactly as defined in eval_cases.json "metrics".
* ``evals.judge``      optional LLM judge with a versioned rubric (never used for hard checks).
* ``evals.runner``     ``python -m evals.runner --suite offline|models|live`` writes one JSONL record per run.
* ``evals.report``     ``python -m evals.report`` writes evals/results/summary.json and docs/EVAL_REPORT.md.
"""

from __future__ import annotations

import re
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVALS_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
RESULTS_DIR = EVALS_DIR / "results"
EVAL_CASES_PATH = REPO_ROOT / "eval_cases.json"
REPORT_PATH = REPO_ROOT / "docs" / "EVAL_REPORT.md"
# JSONL record schema. Bump it whenever a record field is added, removed or changes meaning, and teach
# evals.metrics.upgrade_record how to read the older version (the report reads every schema up to this one).
#   1  original records. Later schema-1 files also carry harness_hash, preferences and message (added without a
#      bump); git_dirty counted any change, including the results being written; prompt_hash covered
#      app/graph/prompts.py only; budget_stop records were always --max-minutes stops.
#   2  harness_hash, preferences and message always present; harness-exception records (status "error", error
#      category "harness_exception", no assertions); git_dirty ignores generated eval output (results/, the
#      report); prompt_hash covers every model-visible text (evals.cases.prompt_hash); budget_stop records carry
#      a reason (max_minutes | max_cost_usd) and the cost spent.
RECORD_SCHEMA = 2
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # results/<UTC date>/; anything else (tmp/) is scratch
