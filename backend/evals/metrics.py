"""Per-model metrics, exactly as defined in eval_cases.json "metrics", computed from JSONL run records.

Every rate carries its numerator and denominator (``n``); a rate with a zero denominator is None ("doesn't apply").
Definitions and the choices this module makes where the spec leaves room:

* hard_pass_rate (headline): runs whose every applicable code assertion passed / runs. Harness errors count as
  failed runs (never rerun or dropped).
* status_accuracy: runs whose final status is in expected_statuses / runs.
* agent_success: runs ending complete in agent mode / runs of feasible cases (complete expected and no_feasible_plan
  not acceptable). Fallbacks do not count. None for the baseline row (no agent).
* first_submission_validity: runs whose first submit_plan was valid / runs with at least one submission.
* repairs_per_plan: mean repairs (submit attempts - 1) over runs that ended complete in agent mode.
* tool_protocol_errors: mean malformed / unknown / schema-invalid tool calls per run. None for the baseline row.
* clarification precision / recall: one "instance" per planner call. Blocking instances are calls on cases whose
  expected_statuses is exactly ["needs_clarification"], plus the first call of a case that supplies
  clarification_answers (the spec provides answers because a question is expected there; its resume call is
  non-blocking). Precision = asked on blocking / all asked; recall = blocking instances that asked / blocking.
* parse_accuracy: matching fields / fields, over runs of cases with parse_expect.
* judge_quality: mean over judged runs of the mean of the four rubric axes (hard failures score 1); None when no
  judge ran. security_pass_rate: hard pass rate on security-category cases.
* latency: p50/p95 (linear interpolation) of end-to-end wall-clock per run, plus p50 per graph node.
* cost_per_plan_usd: mean registry-priced cost per run (local, fake and baseline rows are 0).
* pass_hat_k: when a case ran k > 1 times, cases whose k runs all passed / cases.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from evals.assertions import hard_pass

FRONTEND_FIELDS = ("model", "kind", "runs", "hard_pass_rate", "status_accuracy", "agent_success",
                   "first_submission_validity", "tool_protocol_errors", "clarification_precision",
                   "clarification_recall", "judge_quality", "latency_p50_ms", "latency_p95_ms", "cost_per_plan_usd")
NO_AGENT_KINDS = ("baseline",)


def ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default); None for no values."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def run_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("record_type", "run") == "run"]


def record_hard_pass(rec: dict[str, Any]) -> bool:
    if rec.get("error"):
        return False
    return hard_pass(rec.get("assertions") or [])


def _finalize(rec: dict[str, Any]) -> dict[str, Any]:
    fin = rec.get("finalize")
    return fin if isinstance(fin, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def model_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Metrics for one model's run records (one suite). Keys match the /evals frontend row, plus additive fields."""
    recs = run_records(records)
    kind = str(recs[0].get("kind")) if recs else None
    no_agent = kind in NO_AGENT_KINDS
    runs = len(recs)
    n: dict[str, int] = {"runs": runs}

    hard = [record_hard_pass(r) for r in recs]
    status_ok = [bool(r.get("status_ok")) for r in recs]

    feasible = [r for r in recs if r.get("feasible")]
    agent_ok = [r for r in feasible if r.get("status") == "complete" and r.get("mode") == "agent"]
    n["feasible_runs"] = len(feasible)

    subs = [r for r in recs if _finalize(r).get("first_submit_valid") is not None]
    first_ok = [r for r in subs if _finalize(r).get("first_submit_valid") is True]
    n["submissions"] = len(subs)

    agent_plans = [r for r in recs if r.get("status") == "complete" and r.get("mode") == "agent"
                   and _int(_finalize(r).get("submit_attempts")) >= 1]
    repairs = [float(_int(_finalize(r).get("repairs"))) for r in agent_plans]
    n["agent_plans"] = len(agent_plans)
    protocol = [float(_int(_finalize(r).get("protocol_errors"))) for r in recs]

    instances = [i for r in recs for i in (r.get("clarification") or [])]
    asked = [i for i in instances if i.get("asked")]
    blocking = [i for i in instances if i.get("blocking")]
    n["clarifications_asked"] = len(asked)
    n["blocking_instances"] = len(blocking)

    fields = [f for r in recs for f in ((r.get("parse") or {}).values())]
    n["parse_fields"] = len(fields)

    security = [record_hard_pass(r) for r in recs if r.get("category") == "security"]
    n["security_runs"] = len(security)

    judged = [r["judge"] for r in recs if isinstance(r.get("judge"), dict) and r["judge"].get("status") in
              ("scored", "hard_fail")]
    n["judged_runs"] = len(judged)
    axes: dict[str, list[float]] = defaultdict(list)
    for j in judged:
        for axis, score in (j.get("scores") or {}).items():
            axes[axis].append(float(score))
    judge_models = sorted({str(j.get("model")) for j in judged if j.get("model")})
    rubrics = sorted({str(j.get("rubric_version")) for j in judged if j.get("rubric_version")})

    latencies = [float(r["latency_ms"]) for r in recs if isinstance(r.get("latency_ms"), int | float)]
    n["latency_runs"] = len(latencies)
    costs = [float(((r.get("usage") or {}).get("cost_usd")) or 0.0) for r in recs]
    tokens_in = sum(_int((r.get("usage") or {}).get("input_tokens")) for r in recs)
    tokens_out = sum(_int((r.get("usage") or {}).get("output_tokens")) for r in recs)

    nodes: dict[str, list[float]] = defaultdict(list)
    for r in recs:
        for node, ms in (r.get("node_ms") or {}).items():
            nodes[node].append(float(ms))

    by_case: dict[str, list[bool]] = defaultdict(list)
    for r, h in zip(recs, hard, strict=True):
        by_case[str(r.get("case"))].append(h)
    k = max((len(v) for v in by_case.values()), default=0)
    full = [v for v in by_case.values() if len(v) == k]
    pass_hat_k = ratio(sum(1 for v in full if all(v)), len(full)) if k > 1 else None
    n["pass_hat_k_cases"] = len(full) if k > 1 else 0

    errors: Counter[str] = Counter()
    for r in recs:
        for cat in r.get("error_categories") or []:
            errors[str(cat)] += 1

    row: dict[str, Any] = {
        "model": str(recs[0].get("model")) if recs else None,
        "kind": kind,
        "runs": runs,
        "hard_pass_rate": ratio(sum(hard), runs),
        "status_accuracy": ratio(sum(status_ok), runs),
        "agent_success": None if no_agent else ratio(len(agent_ok), len(feasible)),
        "first_submission_validity": None if no_agent else ratio(len(first_ok), len(subs)),
        "tool_protocol_errors": None if no_agent else mean(protocol),
        "clarification_precision": ratio(sum(1 for i in asked if i.get("blocking")), len(asked)),
        "clarification_recall": ratio(sum(1 for i in blocking if i.get("asked")), len(blocking)),
        "judge_quality": mean([float(j["mean"]) for j in judged if j.get("mean") is not None]),
        "latency_p50_ms": _round(percentile(latencies, 50)),
        "latency_p95_ms": _round(percentile(latencies, 95)),
        "cost_per_plan_usd": round(sum(costs) / runs, 6) if runs else None,
        # additive fields (not rendered by the current /evals table)
        "hard_passes": sum(hard),
        "status_ok_runs": sum(status_ok),
        "agent_successes": None if no_agent else len(agent_ok),
        "first_submit_valid": None if no_agent else len(first_ok),
        "repairs_per_plan": None if no_agent else mean(repairs),
        "clarification_hits": sum(1 for i in asked if i.get("blocking")),
        "blocking_asked": sum(1 for i in blocking if i.get("asked")),
        "parse_accuracy": ratio(sum(1 for f in fields if f.get("match")), len(fields)),
        "parse_matches": sum(1 for f in fields if f.get("match")),
        "pass_hat_k_passes": sum(1 for v in full if all(v)) if k > 1 else None,
        "security_pass_rate": ratio(sum(security), len(security)),
        "security_passes": sum(security),
        "judge_axes": {axis: mean(v) for axis, v in sorted(axes.items())} or None,
        "judge_models": judge_models,
        "judge_rubric_versions": rubrics,
        "pass_hat_k": pass_hat_k,
        "k": k,
        "cases": len(by_case),
        "latency_mean_ms": _round(mean(latencies)),
        "node_latency_p50_ms": {node: _round(percentile(v, 50)) for node, v in sorted(nodes.items())},
        "total_cost_usd": round(sum(costs), 6),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "error_categories": dict(sorted(errors.items())),
        "n": n,
    }
    return row


def _round(value: float | None) -> int | None:
    return None if value is None else round(value)


def frontend_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in FRONTEND_FIELDS}


def by_split(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for split in ("dev", "holdout"):
        subset = [r for r in run_records(records) if r.get("split") == split]
        if subset:
            out[split] = model_metrics(subset)
    return out


def by_category(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in run_records(records):
        groups[str(r.get("category"))].append(r)
    out: dict[str, dict[str, Any]] = {}
    for cat, recs in sorted(groups.items()):
        passes = sum(record_hard_pass(r) for r in recs)
        out[cat] = {"runs": len(recs), "hard_passes": passes, "hard_pass_rate": ratio(passes, len(recs))}
    return out


def failures(records: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """Failed runs with their failing code assertions (case, repeat, assertion, detail)."""
    out: list[dict[str, Any]] = []
    for r in run_records(records):
        if record_hard_pass(r):
            continue
        failed = [a for a in (r.get("assertions") or []) if a.get("kind") == "code" and a.get("passed") is False]
        out.append({
            "model": r.get("model"), "case": r.get("case"), "split": r.get("split"), "category": r.get("category"),
            "repeat": r.get("repeat"), "status": r.get("status"), "mode": r.get("mode"),
            "expected_statuses": r.get("expected_statuses"), "error": r.get("error"),
            "failed": [{"name": a.get("name"), "text": a.get("text"), "detail": a.get("detail")} for a in failed],
        })
        if limit is not None and len(out) >= limit:
            break
    return out
