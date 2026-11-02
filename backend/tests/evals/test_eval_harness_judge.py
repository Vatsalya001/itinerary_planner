"""LLM judge plumbing with a stub model: rubric versioning, hard failures score 1, judge-kind assertions."""

from __future__ import annotations

from typing import Any

from evals.assertions import Check, evaluate
from evals.judge import AXES, RUBRIC_VERSION, AxisScore, Judge, JudgedAssertion, JudgeVerdict
from tests.evals.helpers import observe


class StubJudge:
    def __init__(self, verdict: JudgeVerdict | None = None, error: Exception | None = None) -> None:
        self.calls: list[list[Any]] = []
        self.verdict = verdict
        self.error = error

    async def __call__(self, messages: list[Any]) -> tuple[JudgeVerdict, dict[str, Any]]:
        self.calls.append(messages)
        if self.error:
            raise self.error
        assert self.verdict is not None
        return self.verdict, {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.001, "llm_calls": 1}


def verdict(score: int = 4, **answers: bool) -> JudgeVerdict:
    axis = AxisScore(score=score, reason="ok")
    return JudgeVerdict(fit=axis, practicality=axis, explanation=axis, fun=axis,
                        assertions=[JudgedAssertion(text=t, passed=p, reason="because") for t, p in answers.items()])


async def test_judge_scores_a_passing_plan_and_answers_judge_assertions() -> None:
    obs = await observe("benign_ignore_phrase")
    checks = evaluate(obs)
    text = "Prefers free or low-cost stops"
    stub = StubJudge(JudgeVerdict(fit=AxisScore(score=5), practicality=AxisScore(score=4),
                                  explanation=AxisScore(score=3), fun=AxisScore(score=4),
                                  assertions=[JudgedAssertion(text=text, passed=True, reason="cheap stops")]))
    record, updated = await Judge("anthropic:claude-opus-5-5", None, invoke=stub).score(obs, checks)
    assert record is not None and record["status"] == "scored"
    assert record["model"] == "anthropic:claude-opus-5-5" and record["rubric_version"] == RUBRIC_VERSION
    assert record["scores"] == {"fit": 5, "practicality": 4, "explanation": 3, "fun": 4} and record["mean"] == 4.0
    assert record["usage"]["llm_calls"] == 1
    judged = next(c for c in updated if c.text == text)
    assert judged.kind == "judge" and judged.passed is True and "cheap stops" in judged.detail
    payload = stub.calls[0][1].content
    assert text in payload and "Kasturba" not in stub.calls[0][0].content  # the plan travels as data, not rubric


async def test_hard_failures_score_one_without_calling_the_judge() -> None:
    obs = await observe("assignment_example")
    checks = [*evaluate(obs), Check("plan.cost_within_budget", False, "over budget")]
    stub = StubJudge(verdict(5))
    record, _ = await Judge("anthropic:x", None, invoke=stub).score(obs, checks)
    assert record is not None and record["status"] == "hard_fail"
    assert record["scores"] == dict.fromkeys(AXES, 1) and record["mean"] == 1.0
    assert stub.calls == []


async def test_runs_without_a_plan_are_not_judged_unless_a_plan_was_required() -> None:
    vague = await observe("vague_free_text")
    stub = StubJudge(verdict())
    record, _ = await Judge("anthropic:x", None, invoke=stub).score(vague, evaluate(vague))
    assert record is None and stub.calls == []


async def test_judge_errors_do_not_fail_the_run() -> None:
    obs = await observe("assignment_example")
    checks = evaluate(obs)
    record, updated = await Judge("anthropic:x", None, invoke=StubJudge(error=TimeoutError())).score(obs, checks)
    assert record is not None and record["status"] == "error" and "TimeoutError" in record["error"]
    assert updated == checks


async def test_judge_answers_are_matched_loosely_or_in_order() -> None:
    obs = await observe("benign_ignore_phrase")
    checks = evaluate(obs)
    loose = StubJudge(verdict(4, **{"prefers free or low-cost stops.": False}))
    _, updated = await Judge("j:1", None, invoke=loose).score(obs, checks)
    assert next(c for c in updated if c.kind == "judge").passed is False
    unanswered = StubJudge(verdict(4))
    _, updated = await Judge("j:1", None, invoke=unanswered).score(obs, checks)
    judged = next(c for c in updated if c.kind == "judge")
    assert judged.passed is None and "did not answer" in judged.detail
