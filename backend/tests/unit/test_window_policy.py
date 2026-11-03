"""Window utilization and safe last-venue clipping use synthetic places only."""
from datetime import UTC, datetime

import pytest

from app.domain.fallback import plan_deterministic
from app.domain.fill import cap_final_stop, fill_window
from app.domain.parser_rules import merge_parse, parse_rules
from app.domain.schedule import build_schedule
from app.domain.schemas import Energy, PlanRequest
from app.domain.travel import HeuristicTravel
from app.domain.validator import validate_plan
from tests.factories import make_candidate, make_ctx, make_draft, make_ledger, make_prefs

travel = HeuristicTravel()


@pytest.mark.parametrize("overrun", [1, 10, 15])
def test_last_flexible_venue_is_clipped_with_buffers_preserved(overrun):
    museum = make_candidate("fx:museum", "museum")
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.5, 0))
    ledger = make_ledger(museum, cafe)
    draft = make_draft((museum.id, 90), (cafe.id, 60))
    plan, _ = build_schedule(draft, ledger, make_ctx(), make_prefs(), travel)
    assert plan is not None
    available = plan.totals.elapsed_min - overrun
    ctx, prefs = make_ctx(available=available), make_prefs(available=available, constraints=[])
    clipped = cap_final_stop(draft, ledger, ctx, prefs, [], travel)
    assert clipped.stops[0] == draft.stops[0]
    assert clipped.stops[-1].duration_min == 60 - overrun
    checked = validate_plan(clipped, ledger, ctx, prefs, [], travel)
    assert checked.ok and checked.plan is not None
    assert checked.plan.end_min == ctx.end_min
    assert checked.plan.legs == plan.legs


@pytest.mark.parametrize("duration,available", [(60, 44), (35, 25)])
def test_large_overrun_or_too_short_visit_is_not_clipped(duration, available):
    cafe = make_candidate("fx:cafe", "cafe")
    draft = make_draft((cafe.id, duration))
    assert cap_final_stop(draft, make_ledger(cafe), make_ctx(available=available),
                          make_prefs(available=available), [], travel) is draft


def test_fixed_event_and_overbudget_visit_are_not_repaired_by_clipping():
    gig = make_candidate("fx:gig", "event", event_start_min=900, event_end_min=960)
    draft = make_draft((gig.id, 60))
    assert cap_final_stop(draft, make_ledger(gig), make_ctx(available=50), make_prefs(available=50), [], travel) is draft
    cafe = make_candidate("fx:cafe", "cafe", cost=(1000, 1500))
    draft = make_draft((cafe.id, 60))
    assert cap_final_stop(draft, make_ledger(cafe), make_ctx(available=50),
                          make_prefs(available=50, budget=100), [], travel) is draft


def test_fill_targets_eighty_five_percent_without_overshooting():
    museum = make_candidate("fx:museum", "museum")
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.5, 0))
    ledger = make_ledger(museum, cafe)
    ctx, prefs = make_ctx(energy="medium"), make_prefs(energy="medium", constraints=[])
    filled = fill_window(make_draft((museum.id, 45), (cafe.id, 30)), ledger, ctx, prefs, [], travel)
    checked = validate_plan(filled, ledger, ctx, prefs, [], travel)
    assert checked.ok and checked.plan
    assert 0.85 * 240 <= checked.plan.totals.elapsed_min <= 240


def test_deterministic_search_keeps_a_final_venue_with_small_overrun():
    museum = make_candidate("fx:museum", "museum", duration_range=(60, 60, 60))
    cafe = make_candidate("fx:cafe", "cafe", duration_range=(30, 60, 60))
    ledger = make_ledger(museum, cafe)
    full, _ = build_schedule(make_draft((museum.id, 60), (cafe.id, 60)), ledger, make_ctx(energy="medium"), make_prefs(energy="medium"), travel)
    assert full is not None
    available = full.totals.elapsed_min - 5
    ctx = make_ctx(available=available, energy="medium")
    prefs = make_prefs(available=available, energy="medium", interests=["museums", "coffee"], constraints=[])
    draft = plan_deterministic([museum, cafe], ctx, prefs, [], travel)
    assert draft is not None and len(draft.stops) == 2
    checked = validate_plan(draft, ledger, ctx, prefs, [], travel)
    assert checked.ok and checked.plan and checked.plan.end_min == ctx.end_min
    assert draft.stops[-1].duration_min == 55


def test_no_mood_defaults_to_medium_even_if_model_invents_low_energy():
    request = PlanRequest(free_text="Mumbai, 2000 rupees, four hours. I enjoy museums and coffee.")
    rules = parse_rules(request, datetime(2026,11,3,tzinfo=UTC))
    model = rules.model_copy(deep=True)
    model.preferences.energy = Energy.LOW
    merged = merge_parse(model, rules, request)
    assert merged.preferences.energy == Energy.MEDIUM
    tired = parse_rules(request.model_copy(update={"mood":"I am tired"}), datetime(2026,11,3,tzinfo=UTC))
    assert tired.preferences.energy == Energy.LOW
