"""Metrics math on synthetic JSONL records (definitions in eval_cases.json "metrics")."""

from __future__ import annotations

import pytest

from evals.metrics import by_category, by_split, failures, frontend_row, model_metrics, percentile, ratio
from tests.evals.helpers import record


def test_percentile_linear_interpolation() -> None:
    assert percentile([], 50) is None
    assert percentile([7], 95) == 7
    assert percentile([100, 200, 300, 400], 50) == 250
    assert percentile([100, 200, 300, 400], 95) == pytest.approx(385)
    assert percentile([400, 100, 300, 200], 0) == 100


def test_ratio_is_none_without_a_denominator() -> None:
    assert ratio(0, 0) is None
    assert ratio(1, 3) == 0.3333


def test_headline_rates_and_n() -> None:
    recs = [
        record("a", passed=True),
        record("b", passed=False, status="fallback", mode="deterministic"),
        record("c", passed=True, status="needs_clarification", mode=None, expected=("needs_clarification",),
               first_submit_valid=None, submit_attempts=0),
        record("d", passed=False, error="harness_timeout: no result", status="complete"),
    ]
    row = model_metrics(recs)
    assert row["runs"] == 4 and row["hard_passes"] == 2 and row["hard_pass_rate"] == 0.5  # errors count as failures
    assert row["status_accuracy"] == 0.75 and row["status_ok_runs"] == 3
    # feasible runs: a, b, d (c is a blocking case); complete in agent mode: a and d
    assert row["n"]["feasible_runs"] == 3 and row["agent_successes"] == 2
    assert row["agent_success"] == pytest.approx(0.6667)


def test_agent_metrics_are_not_applicable_to_the_baseline() -> None:
    row = model_metrics([record("a", kind="baseline", model="baseline:deterministic", mode="deterministic",
                                first_submit_valid=None, submit_attempts=0)])
    assert row["agent_success"] is None and row["first_submission_validity"] is None
    assert row["tool_protocol_errors"] is None and row["repairs_per_plan"] is None
    assert row["hard_pass_rate"] == 1.0 and row["cost_per_plan_usd"] == 0.0


def test_submission_validity_repairs_and_protocol_errors() -> None:
    recs = [
        record("a", first_submit_valid=True, submit_attempts=1, protocol_errors=0),
        record("b", first_submit_valid=False, submit_attempts=3, protocol_errors=2),
        record("c", first_submit_valid=None, submit_attempts=0, status="fallback", mode="deterministic",
               protocol_errors=1),
    ]
    row = model_metrics(recs)
    assert row["n"]["submissions"] == 2 and row["first_submission_validity"] == 0.5
    assert row["n"]["agent_plans"] == 2 and row["repairs_per_plan"] == 1.0  # (0 + 2) / 2
    assert row["tool_protocol_errors"] == 1.0  # (0 + 2 + 1) / 3 runs


def test_clarification_precision_and_recall() -> None:
    recs = [
        # blocking case, asked (true positive)
        record("vague", status="needs_clarification", mode=None, expected=("needs_clarification",)),
        # blocking case, not asked (missed)
        record("vague2", status="complete", expected=("needs_clarification",), passed=False),
        # non-blocking case that asked (false positive)
        record("feasible", status="needs_clarification", mode=None, passed=False),
        # ask-then-resume case: first call blocking and asked, resume non-blocking and not asked
        record("answered", clarification=[{"phase": 1, "blocking": True, "asked": True},
                                          {"phase": 2, "blocking": False, "asked": False}]),
    ]
    row = model_metrics(recs)
    assert row["n"]["clarifications_asked"] == 3 and row["clarification_hits"] == 2
    assert row["clarification_precision"] == pytest.approx(0.6667)
    assert row["n"]["blocking_instances"] == 3 and row["blocking_asked"] == 2
    assert row["clarification_recall"] == pytest.approx(0.6667)


def test_no_clarifications_means_precision_is_unmeasured() -> None:
    row = model_metrics([record("a")])
    assert row["clarification_precision"] is None and row["n"]["clarifications_asked"] == 0
    assert row["clarification_recall"] is None


def test_latency_cost_parse_security_and_judge() -> None:
    recs = [
        record("a", latency_ms=1000, cost=0.02, parse={"budget": {"match": True}, "city": {"match": False}},
               judge={"status": "scored", "model": "anthropic:x", "rubric_version": "v1", "mean": 4.5,
                      "scores": {"fit": 5, "practicality": 4, "explanation": 4, "fun": 5}}),
        record("b", latency_ms=3000, cost=0.04, category="security", passed=False,
               judge={"status": "hard_fail", "model": "anthropic:x", "rubric_version": "v1", "mean": 1.0,
                      "scores": {"fit": 1, "practicality": 1, "explanation": 1, "fun": 1}}),
        record("c", latency_ms=2000, category="security", judge={"status": "error", "model": "anthropic:x"}),
    ]
    row = model_metrics(recs)
    assert row["latency_p50_ms"] == 2000 and row["latency_p95_ms"] == 2900
    assert row["cost_per_plan_usd"] == 0.02 and row["total_cost_usd"] == 0.06
    assert row["parse_accuracy"] == 0.5 and row["n"]["parse_fields"] == 2
    assert row["security_pass_rate"] == 0.5 and row["n"]["security_runs"] == 2
    assert row["n"]["judged_runs"] == 2 and row["judge_quality"] == 2.75  # judge errors are excluded
    assert row["judge_axes"] == {"explanation": 2.5, "fit": 3.0, "fun": 3.0, "practicality": 2.5}
    assert row["judge_models"] == ["anthropic:x"] and row["judge_rubric_versions"] == ["v1"]


def test_judge_quality_is_null_without_a_judge() -> None:
    assert model_metrics([record("a")])["judge_quality"] is None


def test_pass_hat_k() -> None:
    recs = [record("a", repeat=i) for i in (1, 2, 3)]
    recs += [record("b", repeat=1), record("b", repeat=2, passed=False), record("b", repeat=3)]
    row = model_metrics(recs)
    assert row["k"] == 3 and row["pass_hat_k"] == 0.5 and row["pass_hat_k_passes"] == 1
    assert row["hard_pass_rate"] == pytest.approx(0.8333)
    assert model_metrics([record("a")])["pass_hat_k"] is None


def test_error_categories_are_counted() -> None:
    recs = [record("a", error_categories=["model_timeout"]), record("b", error_categories=["model_timeout",
                                                                                            "parser_degraded"])]
    assert model_metrics(recs)["error_categories"] == {"model_timeout": 2, "parser_degraded": 1}


def test_frontend_row_has_exactly_the_page_fields() -> None:
    row = frontend_row(model_metrics([record("a")]))
    assert list(row) == ["model", "kind", "runs", "hard_pass_rate", "status_accuracy", "agent_success",
                         "first_submission_validity", "tool_protocol_errors", "clarification_precision",
                         "clarification_recall", "judge_quality", "latency_p50_ms", "latency_p95_ms",
                         "cost_per_plan_usd"]


def test_splits_categories_and_failures() -> None:
    recs = [record("a", split="dev", category="feasible"), record("b", split="holdout", category="weather",
                                                                   passed=False),
            record("c", split="holdout", category="weather")]
    splits = by_split(recs)
    assert splits["dev"]["runs"] == 1 and splits["holdout"]["runs"] == 2 and splits["holdout"]["hard_passes"] == 1
    cats = by_category(recs)
    assert cats["weather"] == {"runs": 2, "hard_passes": 1, "hard_pass_rate": 0.5}
    fails = failures(recs)
    assert [f["case"] for f in fails] == ["b"] and fails[0]["failed"][0]["name"] == "status.expected"


def test_skip_records_are_ignored_by_metrics() -> None:
    recs = [record("a"), {"record_type": "skip", "model": "m:1", "reason": "pending"}]
    assert model_metrics(recs)["runs"] == 1
