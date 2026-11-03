"""fill_window / free_time_note / UNDERFILLED: plans should use the user's window, honestly."""

import time

from app.domain.fallback import plan_deterministic
from app.domain.fill import fill_window, free_time_note
from app.domain.schemas import DraftStop, Hours, PlanDraft
from app.domain.travel import HeuristicTravel
from app.domain.validator import validate_plan
from tests.factories import (
    make_candidate,
    make_ctx,
    make_draft,
    make_ledger,
    make_prefs,
    synthetic_candidates,
)

travel = HeuristicTravel()


def check(draft, cands, ctx, prefs):
    return validate_plan(draft, make_ledger(*cands), ctx, prefs, prefs.hard_constraints, travel)


def jaipur_like():
    museum = make_candidate("fx:museum", "museum", hours=Hours(status="known", periods=[(600, 1080)]))
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(2.5, 1.0))
    return museum, cafe


def test_minimum_durations_get_stretched_to_use_the_window():
    museum, cafe = jaipur_like()
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=[])
    draft = make_draft(("fx:museum", 45), ("fx:cafe", 30))  # both at their minimum, as in the live Jaipur plan
    before = check(draft, [museum, cafe], ctx, prefs)
    assert before.ok and before.plan is not None
    assert before.plan.totals.elapsed_min < 0.6 * 240
    assert "UNDERFILLED" in {v.code for v in before.violations}
    filled = fill_window(draft, make_ledger(museum, cafe), ctx, prefs, [], travel)
    after = check(filled, [museum, cafe], ctx, prefs)
    assert after.ok and after.plan is not None
    assert after.plan.totals.elapsed_min >= 0.75 * 240
    assert after.plan.totals.elapsed_min <= 240
    assert [s.candidate_id for s in filled.stops] == ["fx:museum", "fx:cafe"]  # never reordered or added
    for s, c in zip(filled.stops, (museum, cafe), strict=True):
        assert c.duration_range[0] <= s.duration_min <= c.duration_range[2]
        assert s.why == "A good fit for you."  # prose untouched
    assert "UNDERFILLED" not in {v.code for v in after.violations}
    assert after.plan.totals.worst_case == before.plan.totals.worst_case  # costs don't depend on duration


def test_stretch_respects_opening_hours():
    cafe = make_candidate("fx:cafe", "cafe", hours=Hours(status="known", periods=[(600, 960)]))  # closes 16:00
    gallery = make_candidate("fx:gallery", "art_gallery", offset_km=(0.4, 0.0),
                             hours=Hours(status="known", periods=[(600, 1080)]))
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=[])
    filled = fill_window(make_draft(("fx:cafe", 30), ("fx:gallery", 30)), make_ledger(cafe, gallery), ctx, prefs,
                         [], travel)
    report = check(filled, [cafe, gallery], ctx, prefs)
    assert report.ok and report.plan is not None
    assert report.plan.stops[0].end_min <= 960
    assert report.plan.stops[1].end_min <= 1080
    assert report.plan.totals.elapsed_min > 30 + 30 + 10


def test_stretch_respects_sunset_and_walking_caps():
    park = make_candidate("fx:park", "park")  # walking stop, outdoors; medium energy walking cap is 75 min
    cafe = make_candidate("fx:cafe", "cafe", offset_km=(0.3, 0.0))
    ctx = make_ctx(energy="medium", start_min=1000, available=240, sunset_min=1070)
    prefs = make_prefs(energy="medium", start_min=1000, available=240, constraints=[])
    filled = fill_window(make_draft(("fx:park", 30), ("fx:cafe", 30)), make_ledger(park, cafe), ctx, prefs, [],
                         travel)
    report = check(filled, [park, cafe], ctx, prefs)
    assert report.ok and report.plan is not None
    assert report.plan.stops[0].end_min <= 1070  # no walk after sunset
    assert report.plan.totals.walking_min <= ctx.pacing.max_walk_minutes


def test_events_keep_their_fixed_window():
    cafe = make_candidate("fx:cafe", "cafe")
    gig = make_candidate("ev:gig", "event", offset_km=(0.3, 0.0), event_start_min=1020, event_end_min=1140)
    ctx = make_ctx(energy="medium", available=300)
    prefs = make_prefs(energy="medium", available=300, budget=None, constraints=[])
    draft = make_draft(("fx:cafe", 30), ("ev:gig", 60))
    filled = fill_window(draft, make_ledger(cafe, gig), ctx, prefs, [], travel)
    report = check(filled, [cafe, gig], ctx, prefs)
    assert report.ok and report.plan is not None
    assert filled.stops[1].duration_min == 60
    assert report.plan.stops[1].start_min == 1020


def test_returns_the_original_when_nothing_can_stretch():
    museum, cafe = jaipur_like()
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=[])
    at_max = make_draft(("fx:museum", 120), ("fx:cafe", 90))
    assert fill_window(at_max, make_ledger(museum, cafe), ctx, prefs, [], travel) is at_max
    invalid = make_draft(("gp:unknown", 45))
    assert fill_window(invalid, make_ledger(museum), ctx, prefs, [], travel) is invalid
    gig = make_candidate("ev:gig", "event", event_start_min=900, event_end_min=960)
    only_event = make_draft(("ev:gig", 60))
    assert fill_window(only_event, make_ledger(gig), ctx, make_prefs(energy="medium", budget=None), [],
                       travel) is only_event
    closing = make_candidate("fx:closing", "cafe", hours=Hours(status="known", periods=[(600, 930)]))
    tight = make_draft(("fx:closing", 30))  # closes right after: no valid stretch
    assert fill_window(tight, make_ledger(closing), ctx, prefs, [], travel) is tight


def test_low_energy_stretches_seated_stops_first_and_stays_moderate():
    cafe = make_candidate("fx:cafe", "cafe")  # seated, duration (30, 50, 90)
    gallery = make_candidate("fx:gallery", "art_gallery", offset_km=(0.4, 0.0))  # not seated, (30, 50, 90)
    ctx = make_ctx(energy="low", available=150)
    prefs = make_prefs(energy="low", available=150, constraints=[])
    filled = fill_window(make_draft(("fx:cafe", 30), ("fx:gallery", 30)), make_ledger(cafe, gallery), ctx, prefs,
                         [], travel)
    durs = [s.duration_min for s in filled.stops]
    assert durs[0] >= durs[1]
    assert all(d <= 50 + (90 - 50) // 3 for d in durs)  # low energy: default + a third at most
    assert check(filled, [cafe, gallery], ctx, prefs).ok


def test_fill_is_fast():
    cands = [make_candidate(f"fx:s{i}", key, offset_km=(0.3 * i, 0.0)) for i, key in
             enumerate(["cafe", "museum", "bookstore", "art_gallery", "dessert", "theatre"])]
    ctx = make_ctx(energy="high", start_min=600, available=600)
    prefs = make_prefs(energy="high", start_min=600, available=600, budget=None, constraints=[])
    draft = PlanDraft(stops=[DraftStop(candidate_id=c.id, duration_min=c.duration_range[0], why="Fun.")
                             for c in cands])
    t0 = time.perf_counter()
    filled = fill_window(draft, make_ledger(*cands), ctx, prefs, [], travel)
    assert time.perf_counter() - t0 < 0.5
    assert check(filled, cands, ctx, prefs).ok


# --- UNDERFILLED and free_time_note -------------------------------------------------------------------


def test_underfilled_uses_eighty_percent_margin_including_short_windows():
    museum, cafe = jaipur_like()
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=[])
    low = check(make_draft(("fx:museum", 45), ("fx:cafe", 30)), [museum, cafe], ctx, prefs)
    warning = next(v for v in low.violations if v.code == "UNDERFILLED")
    assert warning.severity == "warning" and low.ok
    from app.domain.validator import summarize_checks

    time_check = next(i for i in summarize_checks(low, ctx, prefs, []) if i.name == "time")
    assert time_check.status == "warn"
    assert low.plan is not None
    free = 240 - low.plan.totals.elapsed_min
    assert time_check.detail.startswith(f"Uses {low.plan.totals.elapsed_min} of 240 min; about")
    assert f"{free // 60} h {free % 60} min left free" in time_check.detail
    high = check(make_draft(("fx:museum", 100), ("fx:cafe", 60)), [museum, cafe], ctx, prefs)
    assert "UNDERFILLED" not in {v.code for v in high.violations}
    short_window = check(make_draft(("fx:cafe", 30)), [cafe], make_ctx(energy="medium", available=80),
                         make_prefs(energy="medium", available=80, constraints=[]))
    assert "UNDERFILLED" in {v.code for v in short_window.violations}


def test_underfilled_is_quiet_when_hours_make_more_impossible():
    closing = make_candidate("fx:closing", "cafe", hours=Hours(status="known", periods=[(600, 940)]))
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=[])
    report = check(make_draft(("fx:closing", 35)), [closing], ctx, prefs)  # 15:00-15:35, closes 15:40
    assert report.ok and "UNDERFILLED" not in {v.code for v in report.violations}


def test_free_time_note():
    museum, cafe = jaipur_like()
    ctx = make_ctx(energy="medium")
    prefs = make_prefs(energy="medium", constraints=[])
    low = check(make_draft(("fx:museum", 45), ("fx:cafe", 30)), [museum, cafe], ctx, prefs)
    assert low.plan is not None
    free = 240 - low.plan.totals.elapsed_min
    from app.domain.timeutil import fmt_clock, fmt_duration

    assert free_time_note(low.plan, ctx) == (
        f"This plan leaves about {fmt_duration(free)} free after {fmt_clock(low.plan.end_min)} — time to rest or "
        "wander nearby.")
    full = check(make_draft(("fx:museum", 110), ("fx:cafe", 70)), [museum, cafe], ctx, prefs)
    assert full.plan is not None and free_time_note(full.plan, ctx) is None


# --- the deterministic planner meets the same bar --------------------------------------------------------


def test_planner_uses_the_window_on_a_jaipur_like_pool():
    museum, cafe = jaipur_like()
    for energy in ("low", "medium"):
        ctx = make_ctx(energy=energy)
        prefs = make_prefs(energy=energy, constraints=[], interests=["museums", "coffee"])
        draft = plan_deterministic([museum, cafe], ctx, prefs, [], travel)
        assert draft is not None
        report = check(draft, [museum, cafe], ctx, prefs)
        assert report.ok and report.plan is not None
        assert "UNDERFILLED" not in {v.code for v in report.violations}, (energy, report.plan.totals)


def test_planner_plans_are_not_underfilled_when_avoidable():
    cands = synthetic_candidates(60)
    for energy, start, available in (("low", 900, 240), ("medium", 900, 240), ("high", 600, 480)):
        ctx = make_ctx(energy=energy, start_min=start, available=available)
        prefs = make_prefs(energy=energy, start_min=start, available=available, budget=None,
                           constraints=["vegetarian"])
        draft = plan_deterministic(cands, ctx, prefs, prefs.hard_constraints, travel)
        assert draft is not None
        report = check(draft, cands, ctx, prefs)
        assert report.ok and "UNDERFILLED" not in {v.code for v in report.violations}, energy
