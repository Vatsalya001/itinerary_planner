"""Regression cases for the assignment completion pass."""
from app.domain.explanations import ground_plan_text
from app.domain.schemas import DraftStop
from app.domain.travel import HeuristicTravel
from app.domain.validator import validate_plan
from tests.factories import make_candidate, make_ctx, make_draft, make_ledger, make_prefs


def test_rain_alternative_is_rejected_when_its_route_breaks_time_and_budget():
    cafe = make_candidate('fx:cafe', 'cafe', cost=100)
    park = make_candidate('fx:park', 'park', cost=0, offset_km=(0.1, 0))
    museum = make_candidate('fx:museum', 'museum', cost=100, offset_km=(25, 0))
    ctx = make_ctx(energy='medium', available=100, weather='rain')
    prefs = make_prefs(energy='medium', available_minutes=100, budget=500)
    draft = make_draft((cafe.id, 30), DraftStop(candidate_id=park.id, duration_min=30,
                                             why='A relaxed pause.', plan_b_candidate_id=museum.id))
    report = validate_plan(draft, make_ledger(cafe, park, museum), ctx, prefs, [], HeuristicTravel())
    assert not report.ok
    assert 'PLAN_B_INVALID' in {v.code for v in report.errors}
    assert any('TIME_EXCEEDED' in v.message or 'BUDGET_EXCEEDED' in v.message for v in report.errors)


def test_model_factual_claims_are_not_used_as_display_evidence():
    cafe = make_candidate('fx:cafe', 'cafe', cost=100)
    ctx = make_ctx(energy='medium')
    prefs = make_prefs(energy='medium', interests=['food'])
    draft = make_draft(DraftStop(candidate_id=cafe.id, duration_min=30,
                                why='Guaranteed vegan menu and wheelchair accessible toilets.',
                                tradeoff='A free concert is guaranteed.',))
    report = validate_plan(draft, make_ledger(cafe), ctx, prefs, [], HeuristicTravel())
    assert report.ok and report.plan is not None
    shown = ground_plan_text(report.plan, prefs, [], ctx)
    assert 'food' in shown.stops[0].why
    assert 'guaranteed' not in shown.model_dump_json().lower()
    assert 'accessible toilets' not in shown.model_dump_json().lower()
    assert 'vegan' not in shown.stops[0].why.lower()
