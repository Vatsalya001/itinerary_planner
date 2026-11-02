"""Validator unit cases (eval_cases.json "validator_unit_cases") plus one scenario per violation code."""

from collections.abc import Callable, Sequence
from datetime import date

import pytest
from pydantic import ValidationError

from app.domain.schemas import (
    Candidate,
    Constraint,
    DraftStop,
    Evidence,
    Hours,
    Omission,
    PlanDraft,
    Preferences,
    Source,
    ValidationReport,
    WorldContext,
)
from app.domain.travel import HeuristicTravel
from app.domain.validator import (
    CODES,
    draft_from_payload,
    numeric_claims,
    summarize_checks,
    url_is_https,
    validate_plan,
)
from tests.factories import (
    MUMBAI,
    make_candidate,
    make_constraints,
    make_ctx,
    make_draft,
    make_ledger,
    make_prefs,
)

travel = HeuristicTravel()


def run(draft: PlanDraft, candidates: Sequence[Candidate], *, ctx: WorldContext | None = None,
        prefs: Preferences | None = None, original: Sequence[str | Constraint] = ()) -> ValidationReport:
    return validate_plan(draft, make_ledger(*candidates), ctx or make_ctx(), prefs or make_prefs(),
                         make_constraints(original), travel)


def codes(report: ValidationReport) -> set[str]:
    return {v.code for v in report.violations}


def stop(cid: str, minutes: int, why: str = "A calm, seated stop.", **kw) -> DraftStop:
    return DraftStop(candidate_id=cid, duration_min=minutes, why=why, **kw)


CAFE = make_candidate("fx:cafe", "cafe", cost=(200, 400))
BOOKS = make_candidate("fx:books", "bookstore", offset_km=(0.4, 0.0), cost=(0, 200))


def test_clean_plan_is_ok():
    report = run(make_draft(("fx:cafe", 50), ("fx:books", 40)), [CAFE, BOOKS],
                 prefs=make_prefs(constraints=["vegetarian", "avoid crowded places"]))
    assert report.ok, report.violations
    assert report.violations == []
    assert report.plan is not None


# --- budget: exactly at budget passes; one rupee over fails -------------------------------------------


def test_budget_exact_passes_one_over_fails_single_stop():
    c = make_candidate("fx:c", "cafe", cost=(1000, 1000))
    exact = run(make_draft(("fx:c", 50)), [c], prefs=make_prefs(budget=1100))  # 1000 + ceil(10%)
    assert exact.ok and exact.plan is not None and exact.plan.totals.worst_case == 1100
    assert exact.plan.totals.budget_left == 0
    over = run(make_draft(("fx:c", 50)), [c], prefs=make_prefs(budget=1099))
    assert not over.ok and "BUDGET_EXCEEDED" in codes(over)


def test_budget_counts_transport_fares():
    a = make_candidate("fx:a", "cafe", cost=(300, 300))
    b = make_candidate("fx:b", "museum", offset_km=(4.0, 0.0), cost=(100, 100))
    probe = run(make_draft(("fx:a", 50), ("fx:b", 60)), [a, b], prefs=make_prefs(budget=None))
    assert probe.plan is not None
    totals = probe.plan.totals
    assert totals.legs_cost_max > 0
    assert totals.worst_case == 400 + totals.legs_cost_max + -(-(400 + totals.legs_cost_max) // 10)
    assert run(make_draft(("fx:a", 50), ("fx:b", 60)), [a, b], prefs=make_prefs(budget=totals.worst_case)).ok
    over = run(make_draft(("fx:a", 50), ("fx:b", 60)), [a, b], prefs=make_prefs(budget=totals.worst_case - 1))
    assert "BUDGET_EXCEEDED" in codes(over)


# --- time: exactly at the limit passes; one minute over fails ------------------------------------------


def test_time_exact_passes_one_minute_over_fails():
    long = make_candidate("fx:long", "spa", cost=(800, 1000), duration_range=(30, 60, 400))
    ok = run(make_draft(("fx:long", 240)), [long], ctx=make_ctx(available=240), prefs=make_prefs(available=240))
    assert ok.ok and ok.plan is not None and ok.plan.totals.minutes_left == 0
    over = run(make_draft(("fx:long", 241)), [long], ctx=make_ctx(available=240), prefs=make_prefs(available=240))
    assert not over.ok and "TIME_EXCEEDED" in codes(over)


def test_time_includes_legs_and_buffers():
    a = make_candidate("fx:a", "cafe")
    b = make_candidate("fx:b", "museum", offset_km=(3.0, 0.0))
    draft = make_draft(("fx:a", 50), ("fx:b", 60))
    probe = run(draft, [a, b], ctx=make_ctx(available=600), prefs=make_prefs(available=600))
    assert probe.plan is not None
    elapsed = probe.plan.totals.elapsed_min
    assert elapsed == 50 + probe.plan.legs[0].minutes_max + 15 + 60  # low-energy buffer is 15
    assert run(draft, [a, b], ctx=make_ctx(available=elapsed), prefs=make_prefs(available=elapsed)).ok
    over = run(draft, [a, b], ctx=make_ctx(available=elapsed - 1), prefs=make_prefs(available=elapsed - 1))
    assert "TIME_EXCEEDED" in codes(over)


def test_time_exceeded_when_window_ends_earlier():
    c = make_candidate("fx:c", "cafe")
    report = run(make_draft(("fx:c", 60)), [c], ctx=make_ctx(available=240, end_min=930), prefs=make_prefs(available=240))
    assert "TIME_EXCEEDED" in codes(report)


# --- opening hours ------------------------------------------------------------------------------------


def test_stop_overlapping_closing_time_fails():
    closes = make_candidate("fx:c", "cafe", hours=Hours(status="known", periods=[(600, 930)]))
    report = run(make_draft(("fx:c", 50)), [closes])
    assert not report.ok and "HOURS_CONFLICT" in codes(report)
    assert report.plan is not None  # still returned for repair feedback


def test_unknown_hours_pass_with_warning():
    unknown = make_candidate("fx:c", "cafe", hours=Hours(status="unknown"))
    report = run(make_draft(("fx:c", 50)), [unknown])
    assert report.ok
    assert [(v.code, v.severity) for v in report.violations] == [("HOURS_UNVERIFIED", "warning")]
    assert report.plan is not None and report.plan.stops[0].warnings
    checks = {c.name: c for c in summarize_checks(report, make_ctx(), make_prefs(), [])}
    assert checks["opening_hours"].status == "unverified"


def test_event_outside_its_window_fails():
    ev = make_candidate("ev:e", "event", event_start_min=900, event_end_min=960)
    report = run(make_draft(("ev:e", 90)), [ev])
    assert "HOURS_CONFLICT" in codes(report)


# --- evidence -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("evidence", [Evidence.UNKNOWN, Evidence.LIKELY])
def test_unknown_diet_cannot_satisfy_strict_diet(evidence):
    r = make_candidate("fx:r", "restaurant", diet={"vegetarian": evidence})
    for diet in ("vegetarian", "vegan", "jain"):
        report = run(make_draft(("fx:r", 60)), [r], prefs=make_prefs(constraints=[f"diet:{diet}"]))
        assert not report.ok
        assert "DIET_UNVERIFIED" in codes(report)


def test_contrary_diet_evidence_conflicts():
    r = make_candidate("fx:r", "restaurant", diet={"vegetarian": Evidence.NO})
    report = run(make_draft(("fx:r", 60)), [r], prefs=make_prefs(constraints=["vegetarian"]))
    assert "DIET_CONFLICT" in codes(report)


@pytest.mark.parametrize("evidence", [Evidence.UNKNOWN, Evidence.LIKELY, Evidence.NO])
def test_unknown_wheelchair_cannot_satisfy_accessibility(evidence):
    c = make_candidate("fx:c", "cafe", wheelchair=evidence)
    report = run(make_draft(("fx:c", 50)), [c], prefs=make_prefs(constraints=["wheelchair accessible"]))
    assert not report.ok and "ACCESSIBILITY_UNVERIFIED" in codes(report)


# --- grounding ----------------------------------------------------------------------------------------


def test_duplicate_stop_fails():
    report = run(make_draft(("fx:cafe", 40), ("fx:cafe", 40)), [CAFE], ctx=make_ctx(energy="medium"))
    assert not report.ok and "DUPLICATE_STOP" in codes(report)


def test_out_of_city_candidate_fails():
    far = make_candidate("fx:mumbai_cafe", "cafe", lat=MUMBAI.lat, lng=MUMBAI.lng)
    report = run(make_draft(("fx:mumbai_cafe", 40)), [far])
    assert not report.ok and "CITY_MISMATCH" in codes(report)


def test_unknown_id_fails_and_never_renders():
    report = run(make_draft(("fx:cafe", 40), ("gp:not_in_ledger", 40)), [CAFE])
    assert not report.ok
    assert "UNKNOWN_ID" in codes(report)
    assert report.plan is None


def test_missing_coordinates_fail():
    nan = make_candidate("fx:nan", "cafe", lat=float("nan"), lng=float("nan"))
    report = run(make_draft(("fx:cafe", 40), ("fx:nan", 40)), [CAFE, nan])
    assert not report.ok and "TRANSFER_UNKNOWN" in codes(report)
    assert report.plan is None


# --- pacing -------------------------------------------------------------------------------------------


def _cluster(n: int) -> list[Candidate]:
    keys = ["cafe", "bookstore", "dessert", "art_gallery", "restaurant", "museum"]
    return [make_candidate(f"fx:s{i}", keys[i], offset_km=(0.2 * i, 0.0)) for i in range(n)]


def test_too_many_stops_for_low_energy():
    cands = _cluster(4)
    draft = make_draft(*[(c.id, c.duration_range[0]) for c in cands])
    low = run(draft, cands, ctx=make_ctx(available=400), prefs=make_prefs(available=400))
    assert any(v.code == "PACING_EXCEEDED" and "stops" in v.message for v in low.violations)
    medium = run(draft, cands, ctx=make_ctx(available=400, energy="medium"), prefs=make_prefs(available=400))
    assert "PACING_EXCEEDED" not in codes(medium)


def test_walking_over_profile_fails():
    cafe = make_candidate("fx:c", "cafe")
    park = make_candidate("fx:p", "park", offset_km=(0.3, 0.0))
    report = run(make_draft(("fx:c", 40), ("fx:p", 60)), [cafe, park])
    assert any(v.code == "PACING_EXCEEDED" and "walking" in v.message for v in report.violations)


def test_too_many_long_legs_fails():
    a = make_candidate("fx:a", "cafe")
    b = make_candidate("fx:b", "restaurant", offset_km=(6.0, 0.0))
    c = make_candidate("fx:c", "dessert", offset_km=(12.0, 0.0))
    report = run(make_draft(("fx:a", 30), ("fx:b", 45), ("fx:c", 20)), [a, b, c],
                 ctx=make_ctx(available=400), prefs=make_prefs(available=400))
    assert any(v.code == "PACING_EXCEEDED" and "legs" in v.message for v in report.violations)


def test_low_energy_needs_a_seated_stop():
    museum = make_candidate("fx:m", "museum")
    report = run(make_draft(("fx:m", 60)), [museum])
    assert any(v.code == "PACING_EXCEEDED" and "seated" in v.message for v in report.violations)


# --- daylight and weather -----------------------------------------------------------------------------


def test_outdoor_walk_after_sunset_fails():
    park = make_candidate("fx:p", "park")
    report = run(make_draft(("fx:p", 40)), [park], ctx=make_ctx(start_min=1050, sunset_min=1070),
                 prefs=make_prefs(start_min=1050, energy="medium"))
    assert "DAYLIGHT_CONFLICT" in codes(report)
    before = run(make_draft(("fx:p", 40)), [park], ctx=make_ctx(start_min=1000, sunset_min=1070, energy="medium"),
                 prefs=make_prefs(start_min=1000))
    assert "DAYLIGHT_CONFLICT" not in codes(before)
    unknown = run(make_draft(("fx:p", 40)), [park], ctx=make_ctx(start_min=1050, sunset_min=None, energy="medium"))
    assert "DAYLIGHT_CONFLICT" not in codes(unknown)
    mall = make_candidate("fx:mall", "mall")  # walking category but indoors
    assert "DAYLIGHT_CONFLICT" not in codes(run(make_draft(("fx:mall", 45)), [mall],
                                                ctx=make_ctx(start_min=1100, energy="medium")))


def test_rain_outdoor_stop_without_plan_b_warns():
    park = make_candidate("fx:p", "park")
    ctx = make_ctx(weather="rain", energy="medium")
    report = run(make_draft(("fx:p", 40)), [park], ctx=ctx)
    assert report.ok
    assert [(v.code, v.severity) for v in report.violations] == [("WEATHER_RISK", "warning")]
    museum = make_candidate("fx:m", "museum", offset_km=(0.5, 0.0))
    draft = PlanDraft(stops=[stop("fx:p", 40, plan_b_candidate_id="fx:m")])
    covered = run(draft, [park, museum], ctx=ctx)
    assert covered.ok and covered.violations == []


def test_invalid_plan_b():
    park = make_candidate("fx:p", "park")
    lake = make_candidate("fx:l", "lake", offset_km=(0.5, 0.0))
    nonveg = make_candidate("fx:nv", "restaurant", diet={"vegetarian": Evidence.NO})
    ctx = make_ctx(weather="rain", energy="medium")
    prefs = make_prefs(constraints=["vegetarian"])
    for plan_b in ("fx:l", "fx:nv", "fx:unknown"):
        draft = PlanDraft(stops=[stop("fx:p", 40, plan_b_candidate_id=plan_b)])
        report = run(draft, [park, lake, nonveg], ctx=ctx, prefs=prefs)
        assert "PLAN_B_INVALID" in codes(report), plan_b
        assert "WEATHER_RISK" in codes(report), plan_b


# --- prose --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Only ₹500 for two", "Rs. 300 a plate", "Rs300 thali", "INR 450 entry", "costs $20", "about £15", "€12 tickets",
    "500 rupees well spent", "rupees 200 only", "a 250/- snack", "spend 45 min here", "a 2 hours stroll",
    "1.5 hrs of music", "a 15-minute walk", "two hours of jazz", "half an hour of browsing", "2h browsing",
    "get there at 3pm", "open till 15:00", "starts 3:30 PM", "by 5 p.m.", "around 7 o'clock",
])
def test_prose_numeric_claims_detected(text):
    assert numeric_claims(text), text
    report = run(PlanDraft(stops=[stop("fx:cafe", 50, why=text)]), [CAFE])
    assert not report.ok and "PROSE_NUMERIC_CLAIM" in codes(report)


@pytest.mark.parametrize("text", [
    "3 stops close together", "rated 4.5 stars", "one of the top 10 cafes", "a calm, seated cafe",
    "Seated and calm for a low-energy start; vegetarian menu confirmed; fits your love of coffee.",
    "Platform 9 and three quarters vibes", "two friends could share a table", "I am happy here",
])
def test_plain_counts_are_fine(text):
    assert numeric_claims(text) == [], text


def test_prose_checked_in_tradeoff_summary_and_omissions():
    draft = PlanDraft(stops=[stop("fx:cafe", 50, tradeoff="A bit pricey at ₹400.")])
    assert "PROSE_NUMERIC_CLAIM" in codes(run(draft, [CAFE]))
    draft = PlanDraft(stops=[stop("fx:cafe", 50)], summary_note="Done by 7pm.")
    assert "PROSE_NUMERIC_CLAIM" in codes(run(draft, [CAFE]))
    draft = PlanDraft(stops=[stop("fx:cafe", 50)], omitted=[Omission(ref="fx:x", reason="Needs 2 hours.")])
    assert "PROSE_NUMERIC_CLAIM" in codes(run(draft, [CAFE]))


# --- events and sources -------------------------------------------------------------------------------


def test_event_date_mismatch():
    wrong = make_candidate("ev:w", "event", event_date=date(2026, 11, 8), event_start_min=900, event_end_min=1020)
    report = run(make_draft(("ev:w", 90)), [wrong])
    assert not report.ok and "EVENT_DATE_MISMATCH" in codes(report)
    undated = make_candidate("ev:u", "event", event_date=None, event_start_min=900, event_end_min=1020)
    assert "EVENT_DATE_MISMATCH" in codes(run(make_draft(("ev:u", 90)), [undated]))
    right = make_candidate("ev:r", "event", event_start_min=900, event_end_min=1020)
    assert run(make_draft(("ev:r", 90)), [right]).ok


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "JavaScript:alert(document.cookie)", " javascript:alert(1)", "http://example.org/e",
    "data:text/html,<script>alert(1)</script>", "//example.org/e", "https://", "https://exa mple.org", "ftp://x.org",
])
def test_unsafe_urls_rejected(url):
    assert not url_is_https(url)
    place = make_candidate("fx:c", "cafe", source=Source(provider="google_places", url=url))
    assert "SOURCE_MISSING" in codes(run(make_draft(("fx:c", 50)), [place]))
    mapped = make_candidate("fx:m", "cafe", maps_url=url)
    assert "SOURCE_MISSING" in codes(run(make_draft(("fx:m", 50)), [mapped]))


def test_https_urls_accepted_and_events_need_one():
    assert url_is_https("https://example.org/events/1")
    assert url_is_https("HTTPS://EXAMPLE.ORG")
    ok = make_candidate("fx:c", "cafe", source=Source(provider="google_places", url="https://maps.google.com/?cid=1"))
    assert run(make_draft(("fx:c", 50)), [ok]).ok
    no_url = make_candidate("ev:n", "event", source=Source(provider="web_event"), event_start_min=900,
                            event_end_min=1020)
    assert "SOURCE_MISSING" in codes(run(make_draft(("ev:n", 90)), [no_url]))


# --- constraints from the raw request -----------------------------------------------------------------


def test_constraint_dropped_by_parser_is_flagged_and_still_enforced():
    nonveg = make_candidate("fx:nv", "restaurant", diet={"vegetarian": Evidence.NO})
    report = run(make_draft(("fx:nv", 60)), [nonveg], prefs=make_prefs(constraints=[]), original=["vegetarian"])
    found = {v.code: v.severity for v in report.violations}
    assert found["CONSTRAINT_DROPPED"] == "warning"
    assert found["DIET_CONFLICT"] == "error"
    assert not report.ok
    veg = make_candidate("fx:v", "restaurant")
    ok = run(make_draft(("fx:v", 60)), [veg], prefs=make_prefs(constraints=[]), original=["vegetarian"])
    assert ok.ok and codes(ok) == {"CONSTRAINT_DROPPED"}


def test_stricter_parsed_diet_is_not_a_drop():
    vegan = make_candidate("fx:v", "restaurant", diet={"vegan": Evidence.CONFIRMED})
    report = run(make_draft(("fx:v", 60)), [vegan], prefs=make_prefs(constraints=["diet:vegan"]),
                 original=["vegetarian"])
    assert report.ok and "CONSTRAINT_DROPPED" not in codes(report)


# --- malformed submissions never render ---------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {},
    {"stops": []},
    {"stops": [{"candidate_id": "fx:cafe"}]},
    {"stops": [{"candidate_id": "fx:cafe", "duration_min": 0, "why": "x"}]},
    {"stops": [{"candidate_id": "fx:cafe", "duration_min": 50, "why": "x" * 301}]},
    {"stops": [{"candidate_id": "fx:cafe", "duration_min": 50, "why": "x", "price": 100}]},
    {"stops": [{"candidate_id": "fx:cafe", "duration_min": "a while", "why": "x"}]},
    {"stops": [{"candidate_id": f"fx:{i}", "duration_min": 30, "why": "x"} for i in range(9)]},
    {"stops": "fx:cafe"},
    "not json",
    '{"stops": [{"candidate_id": "fx:cafe", "duration_min": 50}]}',
])
def test_malformed_draft_is_rejected(payload):
    if isinstance(payload, dict):
        with pytest.raises(ValidationError):
            PlanDraft.model_validate(payload)
    draft, violations = draft_from_payload(payload)
    assert draft is None
    assert [v.code for v in violations] == ["SCHEMA_INVALID"]


def test_wellformed_payload_parses():
    draft, violations = draft_from_payload({"stops": [{"candidate_id": "fx:cafe", "duration_min": 50, "why": "Calm."}]})
    assert draft is not None and violations == []


# --- remaining codes ----------------------------------------------------------------------------------


def test_duration_out_of_range():
    assert "DURATION_OUT_OF_RANGE" in codes(run(make_draft(("fx:cafe", 120)), [CAFE]))
    assert "DURATION_OUT_OF_RANGE" in codes(run(make_draft(("fx:cafe", 20)), [CAFE]))
    ev = make_candidate("ev:e", "event", event_start_min=900, event_end_min=1020)
    assert run(make_draft(("ev:e", 30)), [ev]).ok
    assert "DURATION_OUT_OF_RANGE" in codes(run(make_draft(("ev:e", 20)), [ev]))
    assert "DURATION_OUT_OF_RANGE" in codes(run(make_draft(("ev:e", 150)), [ev]))


def test_alcohol_setting_crowd_and_category_rules():
    bar = make_candidate("fx:bar", "bar")
    assert "ALCOHOL_CONFLICT" in codes(run(make_draft(("fx:bar", 60)), [bar], prefs=make_prefs(constraints=["alcohol:avoid"])))
    resto = make_candidate("fx:r", "restaurant", serves_alcohol=True)
    served = run(make_draft(("fx:r", 60)), [resto], prefs=make_prefs(constraints=["alcohol:avoid"]))
    assert served.ok and {(v.code, v.severity) for v in served.violations} == {("ALCOHOL_SERVED", "warning")}
    park = make_candidate("fx:p", "park")
    assert "SETTING_CONFLICT" in codes(run(make_draft(("fx:cafe", 40), ("fx:p", 30)), [CAFE, park],
                                           prefs=make_prefs(constraints=["indoors only"])))
    busy = make_candidate("fx:busy", "cafe", popularity="high", busy_windows=[(900, 1000)])
    assert "CROWD_LIKELY" in codes(run(make_draft(("fx:busy", 50)), [busy],
                                       prefs=make_prefs(constraints=["avoid crowded places"])))
    mall = make_candidate("fx:mall", "mall")
    assert "MUST_AVOID_VIOLATED" in codes(run(make_draft(("fx:cafe", 40), ("fx:mall", 45)), [CAFE, mall],
                                              prefs=make_prefs(constraints=["must_avoid:mall"])))
    missing = run(make_draft(("fx:cafe", 40)), [CAFE], prefs=make_prefs(constraints=["must_include:sit_down_meal"]))
    assert "MUST_INCLUDE_MISSING" in codes(missing)
    resto_veg = make_candidate("fx:rv", "restaurant")
    assert run(make_draft(("fx:rv", 60)), [resto_veg],
               prefs=make_prefs(constraints=["must_include:sit_down_meal"])).ok


def test_empty_why_is_schema_invalid():
    report = run(PlanDraft(stops=[stop("fx:cafe", 50, why="   ")]), [CAFE])
    assert not report.ok and "SCHEMA_INVALID" in codes(report)


def test_unbuildable_plan_reports_unverified_constraints():
    prefs = make_prefs(constraints=["vegetarian", "wheelchair accessible"])
    report = run(make_draft(("gp:nope", 40)), [CAFE], prefs=prefs)
    items = {i.name: i for i in summarize_checks(report, make_ctx(), prefs, prefs.hard_constraints)}
    assert items["diet"].status == "unverified"
    assert items["accessibility"].status == "unverified"


def test_unknown_category_is_schema_invalid():
    odd = CAFE.model_copy(update={"id": "fx:odd", "category": "spaceport"})
    report = run(make_draft(("fx:odd", 50)), [odd])
    assert "SCHEMA_INVALID" in codes(report)


# --- every code has a scenario ------------------------------------------------------------------------


def _scenarios() -> dict[str, Callable[[], set[str]]]:
    park = make_candidate("fx:p", "park")
    return {
        "SCHEMA_INVALID": lambda: {v.code for v in draft_from_payload({})[1]},
        "UNKNOWN_ID": lambda: codes(run(make_draft(("gp:x", 40)), [CAFE])),
        "CITY_MISMATCH": lambda: codes(run(make_draft(("fx:m", 40)), [
            make_candidate("fx:m", "cafe", lat=MUMBAI.lat, lng=MUMBAI.lng)])),
        "DUPLICATE_STOP": lambda: codes(run(make_draft(("fx:cafe", 40), ("fx:cafe", 40)), [CAFE])),
        "DURATION_OUT_OF_RANGE": lambda: codes(run(make_draft(("fx:cafe", 200)), [CAFE])),
        "BUDGET_EXCEEDED": lambda: codes(run(make_draft(("fx:cafe", 40)), [CAFE], prefs=make_prefs(budget=10))),
        "TIME_EXCEEDED": lambda: codes(run(make_draft(("fx:cafe", 90)), [CAFE], ctx=make_ctx(available=60),
                                           prefs=make_prefs(available=60))),
        "HOURS_CONFLICT": lambda: codes(run(make_draft(("fx:x", 40)), [
            make_candidate("fx:x", "cafe", hours=Hours(status="known", periods=[]))])),
        "HOURS_UNVERIFIED": lambda: codes(run(make_draft(("fx:x", 40)), [
            make_candidate("fx:x", "cafe", hours=Hours())])),
        "DIET_UNVERIFIED": lambda: codes(run(make_draft(("fx:x", 40)), [make_candidate("fx:x", "cafe", diet={})],
                                             prefs=make_prefs(constraints=["vegetarian"]))),
        "DIET_CONFLICT": lambda: codes(run(make_draft(("fx:x", 40)), [
            make_candidate("fx:x", "cafe", diet={"vegetarian": Evidence.NO})], prefs=make_prefs(constraints=["vegetarian"]))),
        "ACCESSIBILITY_UNVERIFIED": lambda: codes(run(make_draft(("fx:x", 40)), [
            make_candidate("fx:x", "cafe", wheelchair=Evidence.UNKNOWN)], prefs=make_prefs(constraints=["access:wheelchair"]))),
        "ALCOHOL_CONFLICT": lambda: codes(run(make_draft(("fx:x", 60)), [make_candidate("fx:x", "bar")],
                                              prefs=make_prefs(constraints=["alcohol:avoid"]))),
        "ALCOHOL_SERVED": lambda: codes(run(make_draft(("fx:x", 60)), [
            make_candidate("fx:x", "restaurant", serves_alcohol=True)], prefs=make_prefs(constraints=["alcohol:avoid"]))),
        "SETTING_CONFLICT": lambda: codes(run(make_draft(("fx:p", 40)), [park],
                                              prefs=make_prefs(constraints=["setting:indoors_only"]))),
        "MUST_INCLUDE_MISSING": lambda: codes(run(make_draft(("fx:cafe", 40)), [CAFE],
                                                  prefs=make_prefs(constraints=["must_include:museum"]))),
        "MUST_AVOID_VIOLATED": lambda: codes(run(make_draft(("fx:cafe", 40)), [CAFE],
                                                 prefs=make_prefs(constraints=["must_avoid:food"]))),
        "CROWD_LIKELY": lambda: codes(run(make_draft(("fx:x", 40)), [
            make_candidate("fx:x", "cafe", popularity="high", busy_windows=[(800, 1000)])],
            prefs=make_prefs(constraints=["crowd:avoid"]))),
        "PACING_EXCEEDED": lambda: codes(run(make_draft(("fx:p", 40)), [park])),
        "DAYLIGHT_CONFLICT": lambda: codes(run(make_draft(("fx:p", 40)), [park], ctx=make_ctx(start_min=1080))),
        "WEATHER_RISK": lambda: codes(run(make_draft(("fx:p", 40)), [park], ctx=make_ctx(weather="rain"))),
        "EVENT_DATE_MISMATCH": lambda: codes(run(make_draft(("ev:x", 60)), [
            make_candidate("ev:x", "event", event_date=date(2026, 11, 14), event_start_min=900)])),
        "SOURCE_MISSING": lambda: codes(run(make_draft(("fx:x", 40)), [
            make_candidate("fx:x", "cafe", source=Source(provider="google_places", url="javascript:alert(1)"))])),
        "TRANSFER_UNKNOWN": lambda: codes(run(make_draft(("fx:cafe", 40), ("fx:x", 40)), [
            CAFE, make_candidate("fx:x", "cafe", lat=float("nan"))])),
        "PROSE_NUMERIC_CLAIM": lambda: codes(run(PlanDraft(stops=[stop("fx:cafe", 40, why="Just ₹200.")]), [CAFE])),
        "CONSTRAINT_DROPPED": lambda: codes(run(make_draft(("fx:cafe", 40)), [CAFE], prefs=make_prefs(),
                                                original=["vegetarian"])),
        "PLAN_B_INVALID": lambda: codes(run(PlanDraft(stops=[stop("fx:p", 40, plan_b_candidate_id="fx:none")]),
                                            [park])),
    }


@pytest.mark.parametrize("code", sorted(CODES))
def test_every_code_is_implemented(code):
    scenarios = _scenarios()
    assert code in scenarios, f"no scenario for {code}"
    assert code in scenarios[code]()


# --- purity and checks panel --------------------------------------------------------------------------


def test_validate_is_pure_and_deterministic():
    ledger = make_ledger(CAFE, BOOKS)
    before = {k: v.model_dump() for k, v in ledger.items()}
    draft = make_draft(("fx:cafe", 50), ("fx:books", 40))
    a = validate_plan(draft, ledger, make_ctx(), make_prefs(), [], travel)
    b = validate_plan(draft, ledger, make_ctx(), make_prefs(), [], travel)
    assert a == b
    assert {k: v.model_dump() for k, v in ledger.items()} == before


def test_summarize_checks_names_and_statuses():
    prefs = make_prefs(constraints=["vegetarian", "avoid crowded places"])
    report = run(make_draft(("fx:cafe", 50), ("fx:books", 40)), [CAFE, BOOKS], prefs=prefs)
    items = summarize_checks(report, make_ctx(), prefs, prefs.hard_constraints)
    assert [i.name for i in items] == ["budget", "time", "opening_hours", "diet", "accessibility", "crowds",
                                       "weather", "sources"]
    assert all(i.status == "pass" for i in items), items
    assert "₹" in items[0].detail


def test_summarize_checks_failures_and_unverified():
    c = make_candidate("fx:c", "cafe", cost=(1000, 1000), diet={})
    prefs = make_prefs(budget=500, constraints=["vegetarian"])
    ctx = make_ctx(weather="unavailable")
    report = validate_plan(make_draft(("fx:c", 50)), make_ledger(c), ctx, prefs, [], travel)
    items = {i.name: i for i in summarize_checks(report, ctx, prefs, prefs.hard_constraints)}
    assert items["budget"].status == "fail"
    assert items["diet"].status == "unverified"
    assert items["weather"].status == "unverified"
    broken = run(make_draft(("gp:nope", 40)), [CAFE])
    items = {i.name: i for i in summarize_checks(broken, make_ctx(), make_prefs(), [])}
    assert items["sources"].status == "fail"
    assert items["budget"].status == "unverified"
