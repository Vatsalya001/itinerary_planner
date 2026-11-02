"""Phase 1 verification: the no-LLM baseline against every applicable case in eval_cases.json.

Requests are built with the file's merge semantics (shallow merge of input_overrides into defaults, arrays
replace, remove_fields deleted, use_defaults=false sends only free_text). Plans are checked by
tests/verification/independent_checks.py, which recomputes cost, time, hours, evidence, pacing, prose,
daylight and Plan B from the raw fixture files, never from the validator.

Skipped (need an LLM fault or the HTTP layer): model_timeout, model_refusal,
invalid_submission_then_repair_exhausted, agent_ends_without_submit, diet_dropped_by_parser,
provider_fallback_chain, rate_limited, oversize_body, xss_in_input, share_link_roundtrip.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.baseline import run_baseline
from app.deps import Deps, build_deps, fixture_settings
from app.domain.parser_rules import apply_answers
from app.domain.schemas import Energy, PlanRequest, PlanResult
from app.providers.fixture import FixtureEvents
from app.trace import Tracer
from tests.verification.independent_checks import (
    Expect,
    cats,
    constraint_keys_from_text,
    events_fixture,
    leaked,
    plan_failures,
    spec,
    truth_for,
    world,
)

SKIP_FAULTS = {
    "llm_timeout", "llm_refusal", "submit_plan_unknown_ids_twice", "text_only_final_turn",
    "parser_omits_diet_and_agent_selects_nonveg", "primary_provider_503", "exceed_per_ip_limit",
}
SKIP_IDS = {"oversize_body", "xss_in_input", "rate_limited", "share_link_roundtrip"}
PROVIDER_FAULTS = {"places_http_503", "events_search_error"}
SENTINELS = {"google_maps_api_key": "sk-ant-SENTINEL-maps-0001", "tavily_api_key": "tvly-SENTINEL-0002"}
BLR, BOM = "fixture:bangalore", "fixture:mumbai"

CASES = {c["id"]: c for c in spec()["cases"]}
APPLICABLE = [cid for cid, c in CASES.items() if c.get("fault") not in SKIP_FAULTS and cid not in SKIP_IDS]

# Expected window/budget derived by reading each case's text (not from the parser).
FREE_TEXT_EXPECT: dict[str, dict[str, Any]] = {
    "free_text_complete": {"budget": 1500, "start_min": 16 * 60, "available": 240},
    "clarification_answered": {"budget": 1000, "start_min": None, "available": 180},
    "holdout_koramangala_brunch": {"budget": 1200, "start_min": 11 * 60, "available": 240},
}
LOCATION_FOR_CITY = {"bangalore": BLR, "bengaluru": BLR, "mumbai": BOM}

# Cases whose baseline result fails an assertion. Empty: every applicable case passes. A new entry must name a
# documented defect in docs/verification/PHASE1.md; it is marked xfail(strict=True), so the fix shows up as an XPASS
# failure and the entry must then be removed.
KNOWN_FAILURES: dict[str, str] = {
}


def build_request_data(case: dict[str, Any]) -> dict[str, Any]:
    if case.get("use_defaults", True) is False:
        data: dict[str, Any] = {"free_text": case["free_text"]}
    else:
        data = dict(spec()["defaults"])
        data.update(case.get("input_overrides", {}))
        if "free_text" in case:
            data["free_text"] = case["free_text"]
    for f in case.get("remove_fields", []):
        data.pop(f, None)
    return data


def make_deps(case: dict[str, Any]) -> Deps:
    kw: dict[str, Any] = dict(SENTINELS)
    fx = case.get("fixtures", {})
    if "weather" in fx:
        kw["fixture_weather"] = fx["weather"]
    if case.get("fault") in PROVIDER_FAULTS:
        kw["fault"] = case["fault"]
    # fixture providers are explicit, so the sentinel keys are never used for a network call
    deps = build_deps(fixture_settings(**kw))
    if "events" in fx:
        deps.events = FixtureEvents(fx["events"])
    return deps


def _minutes(text: str) -> int:
    n = float(text.split()[0])
    return int(n * 60) if "hour" in text else int(n)


def expect_for(case: dict[str, Any], data: dict[str, Any], result: PlanResult) -> Expect:
    city = (data.get("city") or "bangalore").lower()
    if "free_text" in data and not data.get("city"):
        city = "mumbai" if "mumbai" in data["free_text"].lower() else "bangalore"
    loc = LOCATION_FOR_CITY.get(city, BLR)
    texts = list(data.get("constraints", [])) + ([data["free_text"]] if data.get("free_text") else [])
    weather = case.get("fixtures", {}).get("weather", "clear")
    if case["id"] in FREE_TEXT_EXPECT:
        e = FREE_TEXT_EXPECT[case["id"]]
        return Expect(location_id=loc, budget=e["budget"], start_min=e["start_min"], available=e["available"],
                      constraints=constraint_keys_from_text(texts), weather=weather)
    available = _minutes(data["available_time"]) if "available_time" in data else 240
    if "start_time" in data:
        hh, mm = data["start_time"].split(":")
        start = int(hh) * 60 + int(mm)
    else:
        start = 10 * 60 if available > 480 else 15 * 60  # §3 default start rule
    budget = int(data["budget"]) if "budget" in data else None
    return Expect(location_id=loc, budget=budget, start_min=start, available=available,
                  constraints=constraint_keys_from_text(texts), weather=weather)


async def run_case(case: dict[str, Any]) -> tuple[PlanResult, Tracer, dict[str, Any]]:
    data = build_request_data(case)
    request = PlanRequest(**data)
    deps = make_deps(case)
    tracer = Tracer("verify")
    result = await run_baseline(request, deps, tracer=tracer)
    if case.get("clarification_answers"):
        assert result.status == "needs_clarification", "the vague request should ask first"
        answered = apply_answers(request, case["clarification_answers"])
        tracer = Tracer("verify-resume")
        result = await run_baseline(answered, deps, tracer=tracer, asked_before=True)
        data = answered.model_dump(exclude_none=True)
        data["free_text"] = case["free_text"]
    return result, tracer, data


def blob(result: PlanResult, tracer: Tracer) -> str:
    return result.model_dump_json() + json.dumps([e.model_dump(mode="json") for e in tracer.events])


def status_ok(case: dict[str, Any], result: PlanResult) -> bool:
    expected = set(case["expected_statuses"])
    if result.status in expected:
        return True
    # The baseline never reports "fallback"; a deterministic complete satisfies it only for provider degradation.
    return ("fallback" in expected and result.status == "complete" and result.mode == "deterministic"
            and case.get("category") == "degradation")


def case_failures(cid: str, result: PlanResult, tracer: Tracer, data: dict[str, Any]) -> list[str]:
    case = CASES[cid]
    fails: list[str] = []
    if not status_ok(case, result):
        fails.append(f"status {result.status} not in {case['expected_statuses']}")
    text = blob(result, tracer)
    if leaked(text):
        fails.append(f"secret marker in output/trace: {leaked(text)}")
    if result.plan is not None:
        fails += plan_failures(result, expect_for(case, data, result))
    plan = result.plan
    stops = plan.stops if plan else []
    tracer_nodes = [e.node for e in tracer.events if e.type == "node_started"]
    tools = [e.tool for e in tracer.events if e.type == "tool_called"]

    if result.status == "complete":
        for name in ("parse_preferences", "check_clarity", "gather_candidates", "fallback_planner", "validate_plan"):
            if name not in tracer_nodes:
                fails.append(f"trace has no {name} node")
        order = ["parse_preferences", "check_clarity", "gather_candidates", "fallback_planner", "validate_plan"]
        if tracer_nodes != sorted(tracer_nodes, key=lambda n: order.index(n) if n in order else len(order)):
            fails.append(f"trace nodes out of order: {tracer_nodes}")
        if tools[:2] != ["resolve_location", "get_weather"] or "search_places" not in tools:
            fails.append(f"unexpected tool order {tools}")
    if result.status == "needs_clarification":
        if result.plan is not None:
            fails.append("a plan was returned with a clarification")
        if not 1 <= len(result.questions) <= 2:
            fails.append(f"{len(result.questions)} questions")
        if "gather_candidates" in tracer_nodes or "search_places" in tools:
            fails.append("places were searched before clarification")

    # --- case-specific assertions -------------------------------------------------------------------
    food = [s for s in stops if cats()[s.candidate.category]["group"] == "food" or cats()[s.candidate.category]["meal"]]
    if cid == "assignment_example":
        if result.preferences is None or result.preferences.energy != Energy.LOW:
            fails.append("tired mood not read as low energy")
        seated = [t.seated for s in stops if (t := truth_for(s.candidate.id, s.candidate.name)) is not None]
        if len(stops) > 3 or not any(seated):
            fails.append("low energy needs <=3 stops with a seated one")
    if cid == "city_alias_bengaluru" and plan and (
            {s.candidate.city_id for s in stops} != {BLR} or plan.timezone != "Asia/Kolkata"
            or plan.totals.currency != "INR"):
        fails.append("Bengaluru did not resolve like Bangalore")
    if cid == "other_indian_city" and any(s.candidate.id.startswith("fx:blr") for s in stops):
        fails.append("Bangalore candidate in a Mumbai plan")
    if cid == "free_text_complete":
        p = result.preferences
        assert p is not None
        exp = case["parse_expect"]
        got = {"city": p.city, "budget": p.budget, "currency": p.currency, "start": p.start_min,
               "minutes": p.available_minutes, "neighbourhood": p.neighbourhood, "energy": p.energy.value}
        want = {"city": exp["city"], "budget": exp["budget"], "currency": exp["currency"], "start": 16 * 60,
                "minutes": exp["available_minutes"], "neighbourhood": exp["neighbourhood"], "energy": exp["energy"]}
        if got != want:
            fails.append(f"parse mismatch {got} != {want}")
        if not set(exp["hard_constraints"]) <= {c.key for c in p.hard_constraints}:
            fails.append("diet:vegan not parsed")
        if not set(exp["interests_include"]) <= set(p.interests):
            fails.append(f"interests {p.interests}")
        if result.questions:
            fails.append("asked a clarification")
    if cid == "benign_ignore_phrase" and result.questions:
        fails.append("benign 'ignore' phrase triggered a question")
    if cid == "high_energy_long_day":
        if not 4 <= len(stops) <= 6:
            fails.append(f"{len(stops)} stops for a high-energy long day")
        if not any(cats()[s.candidate.category]["meal"] and _in_meal_window(s.start_min) for s in stops):
            fails.append("no meal stop starting inside a normal meal window")
    if cid == "tight_budget_free_activities" and plan:
        if plan.totals.legs_cost_max != sum(leg.fare.max for leg in plan.legs):
            fails.append("leg fares not counted in totals")
        explains = [s.tradeoff or "" for s in stops] + [o.reason for o in plan.omitted]
        if not any("budget" in t.lower() for t in explains):
            fails.append("no trade-off or omission explains what the budget ruled out")
    if cid == "wheelchair_access" and plan:
        decoys = {c["id"] for c in world()["candidates"] if c.get("wheelchair", "unknown") != "confirmed"}
        used = {s.candidate.id for s in stops} | {s.plan_b.id for s in stops if s.plan_b}
        if used & decoys:
            fails.append(f"unknown/no-access candidates used: {used & decoys}")
    if cid == "date_night_no_diet":
        meals = [s for s in stops if cats()[s.candidate.category]["meal"]]
        if not any(19 * 60 + 30 <= s.start_min <= 21 * 60 + 30 for s in meals):
            fails.append(f"no dinner starting 19:30-21:30 ({[(s.candidate.name, s.start_min) for s in meals]})")
        if result.preferences and any(c.kind == "diet" for c in result.preferences.hard_constraints):
            fails.append("a diet restriction was applied")
    if cid in {"vague_free_text", "holdout_vague_short", "missing_city_api"}:
        if not any(q.field == "city" for q in result.questions):
            fails.append("no city question")
        if any(not q.options for q in result.questions if q.id == "city"):
            fails.append("city question without quick replies")
    if cid == "clarification_answered":
        if result.questions:
            fails.append("asked again after answers")
        if not result.assumptions:
            fails.append("no assumptions listed for defaulted soft preferences")
    if cid == "currency_ambiguity_abroad":
        q = next((q for q in result.questions if q.id == "currency"), None)
        if q is None or not {"INR", "GBP"} <= set(q.options):
            fails.append("no INR-or-GBP currency question")
    if cid == "unknown_city":
        q = next((q for q in result.questions if q.id == "city_not_found"), None)
        if q is None or "Atlantisville" not in q.question:
            fails.append("does not say the city was not found")
    if cid == "contradictory_constraints":
        keys = {c.key for c in (result.preferences.hard_constraints if result.preferences else [])}
        if not {"setting:indoors_only", "must_include:outdoor_walk"} <= keys:
            fails.append(f"constraints not both mapped: {keys}")
        q = next((q for q in result.questions if q.id == "conflict"), None)
        if q is None or len(q.options) != 2:
            fails.append("conflict question does not name both sides")
    if cid == "impossible_budget":
        keys = {c.key for c in (result.preferences.hard_constraints if result.preferences else [])}
        if keys != set(case["parse_expect"]["hard_constraints"]):
            fails.append(f"hard constraints {keys}")
        if result.questions:
            fails.append("asked a clarification")
        inf = result.infeasible
        if inf is None or not inf.cheapest_budget or not any("budget" in r.patch for r in inf.relaxations):
            fails.append("no cheapest feasible budget / one-tap budget change")
    if cid == "rain_forecast" and plan:
        if "rain" not in plan.summary_note.lower():
            fails.append("rain not shown in the summary")
        if not any("rain" in s.why.lower() for s in stops):
            fails.append("no why mentions the weather")
    if cid == "weather_unavailable":
        if "Weather not checked" not in result.degradations + result.assumptions:
            fails.append("'Weather not checked' not visible")
        weather_events = [e for e in tracer.events if e.tool == "get_weather"]
        if not any(e.type == "tool_failed" or (e.data or {}).get("recovered") for e in weather_events) and not any(
                e.type == "degraded" and "weather" in (e.summary or "").lower() for e in tracer.events):
            fails.append("trace does not mark get_weather failed and recovered")
    if cid == "event_grounding" and plan:
        valid = events_fixture()["scenarios"]["valid_same_day"]
        for s in stops + [s for s in stops if s.plan_b]:
            c = s.candidate
            if c.kind == "event":
                if c.name != valid["jsonld"]["name"] or c.source.url != valid["page_url"]:
                    fails.append(f"ungrounded event {c.name} {c.source.url}")
                if "check before you go" not in (s.tradeoff or "").lower():
                    fails.append("event stop without the 'check before you go' badge")
    if cid == "places_provider_outage" and plan:
        for s in stops:
            src = s.candidate.source
            if src.provider != "curated_snapshot" or src.last_checked is None:
                fails.append(f"{s.candidate.name}: badge {src.provider} {src.last_checked}")
        if not any("snapshot" in d.lower() for d in result.degradations):
            fails.append("snapshot use not disclosed")
    if cid == "places_outage_no_snapshot_city":
        msg = (result.message or "").lower()
        if "unavailable" not in msg or "try again" not in msg or result.plan is not None:
            fails.append(f"message {result.message!r}")
    if cid == "event_search_failure":
        if any(s.candidate.kind == "event" for s in stops):
            fails.append("event in plan although event search failed")
        if not any("events" in d.lower() for d in result.degradations):
            fails.append("'Couldn't check events' not visible")
    if cid in {"prompt_injection_in_mood", "holdout_injection_variant"} and plan and (
            plan.totals.worst_case > 2000 or (result.preferences and result.preferences.budget != 2000)):
        fails.append("budget of 2000 not enforced")
    if cid == "holdout_injection_variant" and result.preferences and any(
            "SYSTEM" in c.source_text for c in result.preferences.hard_constraints):
        fails.append("injected text became a hard constraint")
    if cid == "prompt_injection_in_event_data":
        inj = events_fixture()["scenarios"]["jsonld_name_with_injected_instructions"]["jsonld"]
        if inj["description"][:40] in text:
            fails.append("event description reached the output")
        for s in stops:
            if s.candidate.kind == "event" and len(s.candidate.name) > 120:
                fails.append("event name longer than 120 characters")
    if cid == "holdout_koramangala_brunch" and plan:
        first = stops[0]
        tags = {t.lower() for t in first.candidate.tags}
        brunchy = first.candidate.category in {"cafe", "restaurant"} and ({"brunch", "breakfast"} & tags)
        if not brunchy or first.start_min != 11 * 60:
            fails.append(f"first stop is {first.candidate.name} [{first.candidate.category}], not brunch at 11:00")
        if plan.end_min > 15 * 60:
            fails.append("ends after 15:00")
    if cid == "holdout_rainy_museum" and plan and not any("rain" in (s.tradeoff or "").lower() for s in stops) \
            and "rain" not in plan.summary_note.lower():
        fails.append("weather not explained in trade-offs")
    if cid == "holdout_jain_diet":
        for s in food:
            t = truth_for(s.candidate.id, s.candidate.name)
            if t is None or t.diet.get("jain") != "confirmed":
                fails.append(f"{s.candidate.name}: no Jain evidence")
    if cid == "holdout_small_budget_music":
        for s in stops:
            if s.candidate.kind == "event" and s.candidate.cost.max > 500:
                fails.append("ticketed event above budget")
    if cid == "holdout_quiet_vs_live_music" and plan:
        notes = " ".join([s.tradeoff or "" for s in stops] + [plan.summary_note]).lower()
        if not ("quiet" in notes or "crowd" in notes or "lively" in notes):
            fails.append("no trade-off names the quiet-vs-live-music compromise")
    if cid == "holdout_family_long_day":
        if len(stops) > 4:
            fails.append(f"{len(stops)} stops")
        if not any(cats()[s.candidate.category]["meal"] and _in_meal_window(s.start_min) for s in stops):
            fails.append(f"no meal starting inside a normal meal window "
                         f"({[(s.candidate.name, s.start_min) for s in stops if cats()[s.candidate.category]['meal']]})")
        if not any("per person" in a for a in result.assumptions):
            fails.append("per-person budget not stated as an assumption")
    return fails


def _in_meal_window(start_min: int) -> bool:
    windows = [(7 * 60 + 30, 10 * 60 + 30), (12 * 60, 14 * 60 + 30), (19 * 60 + 30, 22 * 60)]
    return any(a <= start_min < b for a, b in windows)


def _param(cid: str) -> Any:
    if cid in KNOWN_FAILURES:
        return pytest.param(cid, marks=pytest.mark.xfail(strict=True, reason=KNOWN_FAILURES[cid]))
    return cid


@pytest.mark.parametrize("cid", [_param(c) for c in APPLICABLE if c != "negative_budget"])
async def test_eval_case_baseline(cid: str) -> None:
    result, tracer, data = await run_case(CASES[cid])
    fails = case_failures(cid, result, tracer, data)
    assert not fails, f"{cid}: " + "; ".join(fails)


def test_negative_budget_rejected_before_any_call() -> None:
    data = build_request_data(CASES["negative_budget"])
    with pytest.raises(ValidationError) as exc:
        PlanRequest(**data)
    assert any(err["loc"] == ("budget",) for err in exc.value.errors())


async def test_clarification_answered_does_not_ask_again_even_without_flag() -> None:
    """Stricter than the case: once city/budget/time are answered, nothing should block even if the caller
    forgets ``asked_before``."""
    case = CASES["clarification_answered"]
    request = apply_answers(PlanRequest(**build_request_data(case)), case["clarification_answers"])
    result = await run_baseline(request, make_deps(case))
    assert result.status == "complete", [q.question for q in result.questions]


async def test_impossible_budget_cheapest_budget_is_exact_and_relaxes_nothing_else() -> None:
    case = CASES["impossible_budget"]
    data = build_request_data(case)
    deps = make_deps(case)
    first = await run_baseline(PlanRequest(**data), deps)
    assert first.infeasible is not None and first.infeasible.cheapest_budget
    cheapest = first.infeasible.cheapest_budget
    ok = await run_baseline(PlanRequest(**{**data, "budget": cheapest}), deps)
    assert ok.status == "complete"
    exp = Expect(location_id=BLR, budget=cheapest, start_min=900, available=240,
                 constraints=set(case["parse_expect"]["hard_constraints"]))
    assert plan_failures(ok, exp) == []
    below = await run_baseline(PlanRequest(**{**data, "budget": cheapest - 1}), deps)
    assert below.status == "no_feasible_plan"


async def test_city_alias_matches_canonical_city() -> None:
    base = await run_case(CASES["assignment_example"])
    alias = await run_case(CASES["city_alias_bengaluru"])
    a, b = base[0].plan, alias[0].plan
    assert a is not None and b is not None
    assert (a.city, a.timezone, a.totals.currency) == (b.city, b.timezone, b.totals.currency)
    assert {s.candidate.city_id for s in a.stops} == {s.candidate.city_id for s in b.stops} == {BLR}


async def test_independent_checker_detects_injected_faults() -> None:
    """Sanity check of the harness itself: each injected fault must be reported."""
    case = CASES["assignment_example"]
    result, _, data = await run_case(case)
    exp = expect_for(case, data, result)
    assert plan_failures(result, exp) == []
    assert result.plan is not None

    def failures_after(mutate: Any, expect: Expect = exp) -> list[str]:
        copy = result.model_copy(deep=True)
        mutate(copy.plan)
        return plan_failures(copy, expect)

    def shift(p: Any) -> None:
        p.stops[1].start_min += 5
        p.stops[1].end_min += 5

    def late(p: Any) -> None:
        p.stops[1].start_min, p.stops[1].end_min = 18 * 60, 18 * 60 + p.stops[1].duration_min

    def dup(p: Any) -> None:
        p.stops[2].candidate = p.stops[0].candidate

    assert any("> budget" in f for f in failures_after(lambda p: None, Expect(**{**exp.__dict__, "budget": 500})))
    assert any("recomputed arrival" in f for f in failures_after(shift))
    assert any("currency symbol" in f for f in failures_after(lambda p: setattr(p.stops[0], "why", "Only ₹200.")))
    assert any("word duration" in f for f in failures_after(lambda p: setattr(p.stops[0], "why", "Stay an hour.")))
    assert any("after sunset" in f or "outside hours" in f for f in failures_after(late))
    assert any("duplicate" in f for f in failures_after(dup))
    assert any("vegan evidence" in f for f in failures_after(
        lambda p: None, Expect(**{**exp.__dict__, "constraints": {"diet:vegan"}})))
    assert any("without a Plan B" in f for f in failures_after(
        lambda p: None, Expect(**{**exp.__dict__, "weather": "rain_85pct"})))
