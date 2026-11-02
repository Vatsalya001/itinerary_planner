"""Deterministic preference parser and clarification policy. OWNER: parser agent (Phase 1). Contract only."""

from __future__ import annotations

from datetime import datetime

from app.domain.schemas import ClarificationQuestion, Constraint, Location, ParseOutcome, PlanRequest


def parse_rules(request: PlanRequest, now: datetime) -> ParseOutcome:
    """Regex/rule parser over free_text + structured fields. Structured fields win over free text."""
    raise NotImplementedError


def original_constraints(request: PlanRequest) -> list[Constraint]:
    """Constraints mapped directly from the raw request (fields + free text). Enforced even if a parser
    (LLM or rules) drops them."""
    raise NotImplementedError


def merge_parse(llm: ParseOutcome | None, rules: ParseOutcome, request: PlanRequest) -> ParseOutcome:
    """Combine an LLM parse with the rules parse: explicit request fields win, then LLM, then rules.
    Hard constraints are the union. Defaults applied are recorded in preferences.assumptions."""
    raise NotImplementedError


def clarification_questions(outcome: ParseOutcome, matches: list[Location] | None,
                            asked_before: bool) -> list[ClarificationQuestion]:
    """At most 2 blocking questions; [] when nothing blocks or asked_before is True (then defaults apply).
    Blocking: missing city, city not found (matches == []), ambiguous currency (non-INR country and no
    explicit currency), constraint conflicts, unmappable hard-sounding constraints."""
    raise NotImplementedError


def apply_answers(request: PlanRequest, answers: dict[str, str]) -> PlanRequest:
    """Fold clarification answers (keyed by question id) back into the request."""
    raise NotImplementedError
