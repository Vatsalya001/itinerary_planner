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
RECORD_SCHEMA = 1
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # results/<UTC date>/; anything else (tmp/) is scratch
