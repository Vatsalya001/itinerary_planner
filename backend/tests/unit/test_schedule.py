import math
from urllib.parse import parse_qs, urlsplit

from app.domain.schedule import build_schedule, directions_url
from app.domain.schemas import DraftStop, PlanDraft
from app.domain.travel import HeuristicTravel
from tests.factories import make_candidate, make_ctx, make_draft, make_ledger, make_prefs

travel = HeuristicTravel()


def test_clock_times_legs_and_buffers():
    a = make_candidate("fx:a", "cafe", cost=(200, 400))
    b = make_candidate("fx:b", "museum", offset_km=(3.0, 0.0), cost=(50, 100))
    ctx = make_ctx(energy="low")
    plan, violations = build_schedule(make_draft(("fx:a", 50), ("fx:b", 60)), make_ledger(a, b), ctx,
                                      make_prefs(), travel)
    assert violations == []
    assert plan is not None
    leg = plan.legs[0]
    assert plan.stops[0].start_min == ctx.start_min == 900
    assert plan.stops[0].end_min == 950
    assert plan.stops[1].start_min == 950 + leg.minutes_max + ctx.pacing.buffer_minutes
    assert plan.stops[1].end_min == plan.stops[1].start_min + 60
    t = plan.totals
    assert t.elapsed_min == plan.stops[1].end_min - 900
    assert t.stops_cost_max == 500
    assert t.legs_cost_max == leg.fare.max
    assert t.contingency == math.ceil((500 + leg.fare.max) / 10)
    assert t.worst_case == 500 + leg.fare.max + t.contingency
    assert t.budget == 2000 and t.budget_left == 2000 - t.worst_case
    assert plan.date == ctx.date and plan.city == "Bangalore" and plan.timezone == "Asia/Kolkata"


def test_walking_minutes_count_walk_legs_and_walking_stops():
    a = make_candidate("fx:a", "cafe")
    park = make_candidate("fx:p", "park", offset_km=(0.4, 0.0))
    plan, _ = build_schedule(make_draft(("fx:a", 40), ("fx:p", 35)), make_ledger(a, park), make_ctx(),
                             make_prefs(), travel)
    assert plan is not None
    assert plan.legs[0].mode == "walk"
    assert plan.totals.walking_min == plan.legs[0].minutes + 35


def test_event_waits_for_start_and_waiting_counts():
    cafe = make_candidate("fx:a", "cafe")
    gig = make_candidate("ev:gig", "event", offset_km=(0.3, 0.0), event_start_min=1080, event_end_min=1200)
    plan, violations = build_schedule(make_draft(("fx:a", 40), ("ev:gig", 120)), make_ledger(cafe, gig),
                                      make_ctx(available=360), make_prefs(available=360), travel)
    assert violations == []
    assert plan is not None
    assert plan.stops[1].start_min == 1080
    assert plan.totals.elapsed_min == 1200 - 900


def test_first_stop_event_starts_at_event_time():
    gig = make_candidate("ev:gig", "event", event_start_min=960, event_end_min=1080)
    plan, violations = build_schedule(make_draft(("ev:gig", 120)), make_ledger(gig), make_ctx(), make_prefs(), travel)
    assert violations == [] and plan is not None
    assert plan.stops[0].start_min == 960


def test_late_arrival_to_event_is_hours_conflict_but_plan_returned():
    cafe = make_candidate("fx:a", "cafe")
    gig = make_candidate("ev:gig", "event", offset_km=(0.3, 0.0), event_start_min=930, event_end_min=1050)
    plan, violations = build_schedule(make_draft(("fx:a", 60), ("ev:gig", 60)), make_ledger(cafe, gig),
                                      make_ctx(), make_prefs(), travel)
    assert plan is not None
    assert [v.code for v in violations] == ["HOURS_CONFLICT"]
    assert violations[0].stop_index == 1


def test_unknown_id_returns_no_plan():
    a = make_candidate("fx:a", "cafe")
    plan, violations = build_schedule(make_draft(("fx:a", 40), ("gp:invented", 40)), make_ledger(a), make_ctx(),
                                      make_prefs(), travel)
    assert plan is None
    assert [(v.code, v.stop_index, v.severity) for v in violations] == [("UNKNOWN_ID", 1, "error")]


def test_missing_coordinates_return_no_plan():
    a = make_candidate("fx:a", "cafe")
    b = make_candidate("fx:b", "cafe", lat=float("nan"), lng=float("nan"))
    plan, violations = build_schedule(make_draft(("fx:a", 40), ("fx:b", 40)), make_ledger(a, b), make_ctx(),
                                      make_prefs(), travel)
    assert plan is None
    assert [v.code for v in violations] == ["TRANSFER_UNKNOWN"]


def test_plan_b_resolved_from_ledger():
    park = make_candidate("fx:p", "park")
    museum = make_candidate("fx:m", "museum", offset_km=(0.5, 0.0))
    draft = PlanDraft(stops=[DraftStop(candidate_id="fx:p", duration_min=40, why="Fresh air.",
                                       plan_b_candidate_id="fx:m")])
    plan, _ = build_schedule(draft, make_ledger(park, museum), make_ctx(weather="rain"), make_prefs(), travel)
    assert plan is not None and plan.stops[0].plan_b is not None
    assert plan.stops[0].plan_b.id == "fx:m"
    unknown = PlanDraft(stops=[DraftStop(candidate_id="fx:p", duration_min=40, why="Fresh air.",
                                         plan_b_candidate_id="fx:nope")])
    plan2, v2 = build_schedule(unknown, make_ledger(park), make_ctx(), make_prefs(), travel)
    assert plan2 is not None and plan2.stops[0].plan_b is None and v2 == []


def test_maps_directions_url():
    a = make_candidate("fx:a", "cafe")
    b = make_candidate("fx:b", "museum", offset_km=(2.0, 0.0))
    c = make_candidate("fx:c", "park", offset_km=(3.0, 1.0))
    d = make_candidate("fx:d", "restaurant", offset_km=(1.0, 2.0))
    url = directions_url([a, b, c, d])
    assert url is not None
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.google.com", "/maps/dir/")
    q = parse_qs(parts.query)
    assert q["api"] == ["1"]
    assert q["origin"] == [f"{a.lat:.6f},{a.lng:.6f}"]
    assert q["destination"] == [f"{d.lat:.6f},{d.lng:.6f}"]
    assert q["waypoints"] == [f"{b.lat:.6f},{b.lng:.6f}|{c.lat:.6f},{c.lng:.6f}"]
    assert q["travelmode"] == ["driving"]
    assert "|" not in url and "," not in url  # URL-encoded
    single = parse_qs(urlsplit(directions_url([a]) or "").query)
    assert "origin" not in single and single["destination"] == [f"{a.lat:.6f},{a.lng:.6f}"]
    assert directions_url([]) is None


def test_plan_carries_route_url_and_draft_text():
    a = make_candidate("fx:a", "cafe")
    b = make_candidate("fx:b", "bookstore", offset_km=(0.5, 0.0))
    plan, _ = build_schedule(make_draft(("fx:a", 40), ("fx:b", 30), summary_note="Easy afternoon."),
                             make_ledger(a, b), make_ctx(), make_prefs(), travel)
    assert plan is not None
    assert plan.maps_route_url and plan.maps_route_url.startswith("https://www.google.com/maps/dir/?api=1")
    assert plan.summary_note == "Easy afternoon."
    assert plan.stops[0].why == "A good fit for you."
