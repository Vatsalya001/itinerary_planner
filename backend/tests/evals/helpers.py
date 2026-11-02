"""Shared helpers for the eval-harness tests (offline: fake model, frozen world, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals import RECORD_SCHEMA
from evals.assertions import SENTINEL_SETTINGS, Observation
from evals.cases import EvalCase, load_cases, load_spec
from evals.runner import Session, run_graph_case

SPEC = load_spec()
CASES = {c.id: c for c in load_cases(SPEC)}


def session(tmp_path: Path | None = None, suite: str = "offline") -> Session:
    return Session(id="20261107T000000Z-test", suite=suite, spec=load_spec(), out_dir=tmp_path or Path("/nonexistent"),
                   date="2026-11-07", extra=dict(SENTINEL_SETTINGS), secrets=sorted(SENTINEL_SETTINGS.values()))


async def observe(case_id: str, model: str = "fake:agent", kind: str = "fake") -> Observation:
    from evals.cases import request_data

    case: EvalCase = CASES[case_id]
    sess = session()
    return await run_graph_case(case, model, kind, request_data(case, sess.spec), sess)


def record(case: str = "c1", *, model: str = "m:1", kind: str = "local", status: str = "complete",
           mode: str | None = "agent", passed: bool = True, expected: tuple[str, ...] = ("complete",),
           split: str = "dev", category: str = "feasible", repeat: int = 1, session_id: str = "20261107T000000Z-a",
           suite: str = "models", latency_ms: int = 1000, cost: float = 0.0, first_submit_valid: bool | None = True,
           submit_attempts: int = 1, protocol_errors: int = 0, clarification: list[dict[str, Any]] | None = None,
           parse: dict[str, Any] | None = None, judge: dict[str, Any] | None = None, error: str | None = None,
           error_categories: list[str] | None = None, feasible: bool | None = None) -> dict[str, Any]:
    """A synthetic JSONL run record with just the fields metrics and the report read."""
    return {
        "record_type": "run", "schema": RECORD_SCHEMA, "session": session_id, "suite": suite, "case": case,
        "split": split, "category": category, "repeat": repeat, "model": model, "kind": kind, "status": status, "mode": mode,
        "expected_statuses": list(expected), "status_ok": status in expected,
        "feasible": ("complete" in expected and "no_feasible_plan" not in expected) if feasible is None else feasible,
        "blocking": tuple(expected) == ("needs_clarification",),
        "hard_pass": passed and not error,
        "assertions": [{"name": "status.expected", "passed": passed, "detail": "synthetic", "kind": "code"},
                       {"name": "case.1.judge_only", "passed": None, "detail": "", "kind": "judge"}],
        "finalize": {"first_submit_valid": first_submit_valid, "submit_attempts": submit_attempts,
                     "repairs": max(0, submit_attempts - 1), "protocol_errors": protocol_errors},
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": cost},
        "latency_ms": latency_ms, "clarification": clarification if clarification is not None else [
            {"phase": 1, "blocking": tuple(expected) == ("needs_clarification",),
             "asked": status == "needs_clarification"}],
        "parse": parse, "node_ms": {"plan_agent": latency_ms // 2}, "judge": judge, "error": error,
        "error_categories": error_categories or [], "timestamp": "2026-11-07T00:00:00+00:00",
        "git_sha": "abc123", "git_dirty": False, "prompt_hash": "p", "registry_hash": "r", "fixture_hash": "f",
        "eval_cases_hash": "e", "harness_hash": "h", "preferences": None, "message": None,
    }
