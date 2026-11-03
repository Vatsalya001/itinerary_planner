"""Regressions for reported production failures
    provider payloads are synthetic, not copied Google data."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.evidence import candidate_allowed
from app.domain.explanations import ground_plan_text
from app.domain.fallback import plan_deterministic
from app.domain.realworld import admission_problem, policy_for, prefers_short_travel
from app.domain.schedule import build_schedule, directions_url
from app.domain.schemas import Constraint, CostRange
from app.domain.travel import HeuristicTravel
from app.domain.validator import summarize_checks, validate_plan
from app.providers.google_places import place_to_candidate
from tests.factories import LONDON, MUMBAI, make_candidate, make_ctx, make_draft, make_ledger, make_prefs


def test_muso_admission_is_rejected_by_prefilter_and_final_validator():
    ctx = make_ctx(location=MUMBAI)
    c = make_candidate('fx:restricted', 'museum', name='Museum of Solutions (MuSo)',
                       lat=MUMBAI.lat, lng=MUMBAI.lng, city_id=MUMBAI.id)
    assert 'accompanying child' in (admission_problem(c, ctx) or '')
    assert candidate_allowed(c, [], ctx)[0] is False
    report = validate_plan(make_draft((c.id, 60)), make_ledger(c), ctx, make_prefs(), [], HeuristicTravel())
    assert not report.ok
    assert 'ADMISSION_UNVERIFIED' in {v.code for v in report.violations}
    assert plan_deterministic([c], ctx, make_prefs(), [], HeuristicTravel()) is None


def test_policy_does_not_match_same_name_in_another_city_or_unrelated_museum():
    c = make_candidate(name='Museum of Solutions (MuSo)')
    assert admission_problem(c, make_ctx()) is None
    assert admission_problem(c.model_copy(update={'name':'Museum of Mumbai'}), make_ctx(location=MUMBAI)) is None
    assert policy_for(make_candidate(name='National Portrait Gallery'), make_ctx(location=LONDON)) is None


def test_child_focused_venue_requires_verified_party_eligibility():
    c = make_candidate(tags=['audience:children'])
    assert admission_problem(c, make_ctx()) is not None
    named = make_candidate("fx:child", "museum", name="Example Children\'s Museum")
    assert admission_problem(named, make_ctx()) is not None


def test_national_gallery_general_admission_is_free_with_policy_tag():
    ctx = make_ctx(location=LONDON)
    payload = {'id':'synthetic-gallery', 'displayName':{'text':'The National Gallery'},
               'primaryType':'museum', 'types':['museum'],
               'location':{'latitude':LONDON.lat,'longitude':LONDON.lng}}
    c = place_to_candidate(payload, ctx, 'museum')
    assert c is not None
    assert c.cost == CostRange(min=0, max=0, currency='GBP', basis='free')
    assert 'admission:general_free' in c.tags
    unrelated = place_to_candidate({**payload, 'displayName':{'text':'Another Gallery'}}, ctx, 'museum')
    assert unrelated is not None and unrelated.cost.basis != 'free'


@pytest.mark.parametrize('a,b', [(18.99,19.06),(19.06,19.2)])
def test_mumbai_motor_legs_conservatively_use_cabs_even_in_suburbs(a,b):
    ctx = make_ctx(location=MUMBAI)
    first = make_candidate(lat=a,lng=72.83)
    second = make_candidate('fx:b',lat=b,lng=72.83)
    leg = HeuristicTravel().leg(first,second,ctx)
    assert leg.mode=='cab' and leg.fare.currency=='INR' and leg.fare.max>0


def test_walking_route_link_and_mixed_mode_links():
    a=make_candidate()
    b=make_candidate('fx:b',offset_km=(0.4,0))
    c=make_candidate('fx:c',offset_km=(4,0))
    plan, _=build_schedule(make_draft((a.id,40),(b.id,40)),make_ledger(a,b),make_ctx(),make_prefs(),HeuristicTravel())
    assert plan is not None and plan.maps_route_url
    assert parse_qs(urlsplit(plan.maps_route_url).query)['travelmode']==['walking']
    motor=HeuristicTravel().leg(b,c,make_ctx())
    assert directions_url([a,b,c],[plan.legs[0],motor]) is None


def test_accessibility_scope_is_unverified_and_exportable_not_a_full_pass():
    con=Constraint(kind='access',value='wheelchair')
    prefs=make_prefs(hard_constraints=[con])
    a=make_candidate()
    b=make_candidate('fx:b',offset_km=(0.4,0))
    report=validate_plan(make_draft((a.id,40),(b.id,40)),make_ledger(a,b),make_ctx(),prefs,[con],HeuristicTravel())
    assert report.ok and report.plan
    check=next(c for c in summarize_checks(report,make_ctx(),prefs,[con]) if c.name=='accessibility')
    assert check.status=='unverified' and 'entrances' in check.detail and 'routes' in check.detail
    assert any(v.code=='ACCESS_SCOPE_UNVERIFIED' for v in report.warnings)
    assert 'wheelchair-route' in report.plan.stops[0].warnings[0]


def test_generic_real_venue_admission_is_provisional_not_budget_confirmed():
    c=make_candidate('gp:synthetic','museum',seated=True)
    c=c.model_copy(update={'source':c.source.model_copy(update={'provider':'google_places',
                            'attribution':'Google Maps','url':'https://example.org/synthetic'})})
    prefs=make_prefs(interests=['museums'])
    report=validate_plan(make_draft((c.id,60)),make_ledger(c),make_ctx(),prefs,[],HeuristicTravel())
    assert report.ok and report.plan
    assert any(v.code=='PRICE_UNVERIFIED' for v in report.warnings)
    assert next(x for x in summarize_checks(report,make_ctx(),prefs,[]) if x.name=='budget').status=='unverified'
    grounded=ground_plan_text(report.plan,prefs,[],make_ctx())
    assert 'ticket price' in (grounded.stops[0].tradeoff or '')


def test_short_travel_preference_has_explicit_tradeoff_for_long_leg():
    prefs=make_prefs(soft_preferences=['keep travel short'],interests=['coffee','museums'])
    assert prefers_short_travel(prefs)
    a=make_candidate()
    b=make_candidate('fx:b','museum',offset_km=(5,0),seated=True)
    report=validate_plan(make_draft((a.id,40),(b.id,60)),make_ledger(a,b),make_ctx(),prefs,[],HeuristicTravel())
    assert any(v.code=='LONG_TRANSFER' for v in report.warnings)


def test_short_travel_option_prefers_nearby_interest_match():
    prefs = make_prefs(soft_preferences=["keep travel short"], interests=["coffee", "museums"])
    a = make_candidate("fx:cafe", "cafe")
    nearby = make_candidate("fx:near", "museum", offset_km=(0.3, 0), seated=True)
    distant = make_candidate("fx:far", "museum", offset_km=(6, 0), seated=True, rating=5)
    draft = plan_deterministic([a, nearby, distant], make_ctx(), prefs, [], HeuristicTravel())
    assert draft is not None
    ids = {s.candidate_id for s in draft.stops}
    assert "fx:near" in ids and "fx:cafe" in ids and "fx:far" not in ids


def test_restricted_venue_cannot_be_used_as_rain_plan_b():
    ctx = make_ctx(location=MUMBAI, weather="rain")
    a = make_candidate("fx:out", "park", lat=MUMBAI.lat, lng=MUMBAI.lng, city_id=MUMBAI.id)
    b = make_candidate("fx:child", "museum", name="Museum of Solutions (MuSo)",
                       lat=MUMBAI.lat, lng=MUMBAI.lng, city_id=MUMBAI.id)
    draft = make_draft((a.id, 40))
    draft.stops[0].plan_b_candidate_id = b.id
    report = validate_plan(draft, make_ledger(a, b), ctx, make_prefs(), [], HeuristicTravel())
    assert not report.ok
    assert any(v.code == "PLAN_B_INVALID" and "accompanying child" in v.message for v in report.violations)
