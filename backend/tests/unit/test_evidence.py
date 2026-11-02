import pytest

from app.domain.evidence import (
    busy_most_of_day,
    candidate_allowed,
    check_constraint,
    is_food_stop,
    likely_busy,
    matches_avoid,
    opening_status,
    satisfies_include,
)
from app.domain.schemas import Evidence, Hours
from tests.factories import TARGET_DATE, make_candidate, make_constraints, make_ctx

CTX = make_ctx()


def con(key: str):
    return make_constraints([key])[0]


# --- opening hours ---------------------------------------------------------------------------------


def test_opening_status_known_periods():
    c = make_candidate("fx:c", "cafe", hours=Hours(status="known", periods=[(600, 960), (1080, 1320)]))
    assert opening_status(c, 900, 960) == "open"  # ends exactly at closing
    assert opening_status(c, 900, 961) == "closed"  # overlaps closing
    assert opening_status(c, 950, 1100) == "closed"  # spans the afternoon gap
    assert opening_status(c, 1100, 1200) == "open"
    assert opening_status(c, 500, 620) == "closed"


def test_opening_status_closed_all_day_and_unknown():
    closed = make_candidate("fx:x", "cafe", hours=Hours(status="known", periods=[]))
    assert opening_status(closed, 900, 950) == "closed"
    unknown = make_candidate("fx:u", "cafe", hours=Hours(status="unknown"))
    assert opening_status(unknown, 900, 950) == "unknown"


def test_opening_status_after_midnight():
    bar = make_candidate("fx:bar", "bar", hours=Hours(status="known", periods=[(1080, 120)]))
    assert opening_status(bar, 1400, 1500) == "open"
    assert opening_status(bar, 1500, 1570) == "closed"


def test_event_open_exactly_within_window():
    ev = make_candidate("ev:gig", "event", event_start_min=1020, event_end_min=1140)
    assert opening_status(ev, 1020, 1140) == "open"
    assert opening_status(ev, 1020, 1080) == "open"
    assert opening_status(ev, 1000, 1080) == "closed"
    assert opening_status(ev, 1030, 1150) == "closed"
    assert opening_status(make_candidate("ev:x", "event", event_start_min=None, event_end_min=None), 900, 960) \
        == "unknown"


# --- crowds ----------------------------------------------------------------------------------------


def test_likely_busy_needs_high_popularity_and_overlap():
    high = make_candidate("fx:h", "cafe", popularity="high", busy_windows=[(1020, 1200)])
    assert likely_busy(high, 1000, 1030)
    assert not likely_busy(high, 900, 1020)  # touching the window is not overlapping
    medium = make_candidate("fx:m", "cafe", popularity="medium", busy_windows=[(1020, 1200)])
    assert not likely_busy(medium, 1000, 1100)


def test_crowd_constraint_at_scheduled_time():
    high = make_candidate("fx:h", "cafe", popularity="high", busy_windows=[(1020, 1200)])
    busy = check_constraint(high, con("crowd:avoid"), 1030, 1080, CTX)
    assert not busy.satisfied and busy.code == "CROWD_LIKELY"
    calm = check_constraint(high, con("crowd:avoid"), 900, 960, CTX)
    assert calm.satisfied and calm.code is None


def test_busy_most_of_day_prefilter():
    market = make_candidate("fx:mk", "market", popularity="high", busy_windows=[(600, 1260)])
    assert busy_most_of_day(market, CTX)
    allowed, reasons = candidate_allowed(market, make_constraints(["crowd:avoid"]), CTX)
    assert not allowed and reasons
    evening_only = make_candidate("fx:e", "bar", popularity="high", busy_windows=[(1200, 1440)])
    assert not busy_most_of_day(evening_only, CTX)
    assert candidate_allowed(evening_only, make_constraints(["crowd:avoid"]), CTX)[0]


# --- diet ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("diet", "constraint", "satisfied", "code"),
    [
        ({"vegetarian": Evidence.CONFIRMED}, "diet:vegetarian", True, None),
        ({"vegan": Evidence.CONFIRMED}, "diet:vegetarian", True, None),
        ({"jain": Evidence.CONFIRMED}, "diet:vegetarian", True, None),
        ({}, "diet:vegetarian", False, "DIET_UNVERIFIED"),
        ({"vegetarian": Evidence.UNKNOWN}, "diet:vegetarian", False, "DIET_UNVERIFIED"),
        ({"vegetarian": Evidence.LIKELY}, "diet:vegetarian", False, "DIET_UNVERIFIED"),
        ({"vegetarian": Evidence.NO}, "diet:vegetarian", False, "DIET_CONFLICT"),
        ({"vegetarian": Evidence.CONFIRMED}, "diet:vegan", False, "DIET_UNVERIFIED"),
        ({"vegan": Evidence.CONFIRMED}, "diet:vegan", True, None),
        ({"vegan": Evidence.NO}, "diet:vegan", False, "DIET_CONFLICT"),
        ({"vegetarian": Evidence.NO}, "diet:vegan", False, "DIET_CONFLICT"),
        ({"vegetarian": Evidence.CONFIRMED}, "diet:jain", False, "DIET_UNVERIFIED"),
        ({"jain": Evidence.CONFIRMED}, "diet:jain", True, None),
        ({"jain": Evidence.LIKELY}, "diet:jain", False, "DIET_UNVERIFIED"),
    ],
)
def test_diet_evidence(diet, constraint, satisfied, code):
    c = make_candidate("fx:r", "restaurant", diet=diet)
    chk = check_constraint(c, con(constraint), 900, 960, CTX)
    assert chk.satisfied is satisfied
    assert chk.code == code


def test_diet_applies_only_to_food_stops():
    park = make_candidate("fx:p", "park", diet={})
    assert not is_food_stop(park)
    assert check_constraint(park, con("diet:vegan"), 900, 960, CTX).satisfied
    brewery = make_candidate("fx:b", "brewery", diet={})
    assert is_food_stop(brewery)
    assert not check_constraint(brewery, con("diet:vegetarian"), 900, 960, CTX).satisfied
    for key in ("cafe", "dessert", "street_food", "restaurant"):
        assert is_food_stop(make_candidate("fx:f", key))


# --- access, alcohol, setting ----------------------------------------------------------------------


@pytest.mark.parametrize(("evidence", "ok"), [(Evidence.CONFIRMED, True), (Evidence.LIKELY, False),
                                              (Evidence.UNKNOWN, False), (Evidence.NO, False)])
def test_wheelchair_needs_confirmed_for_every_stop(evidence, ok):
    for key in ("cafe", "park", "museum"):
        chk = check_constraint(make_candidate("fx:w", key, wheelchair=evidence), con("access:wheelchair"),
                               900, 960, CTX)
        assert chk.satisfied is ok
        assert chk.code == (None if ok else "ACCESSIBILITY_UNVERIFIED")


def test_alcohol_avoid():
    bar = make_candidate("fx:bar", "bar")
    chk = check_constraint(bar, con("alcohol:avoid"), 900, 960, CTX)
    assert not chk.satisfied and chk.code == "ALCOHOL_CONFLICT"
    resto = make_candidate("fx:r", "restaurant", serves_alcohol=True)
    warn = check_constraint(resto, con("alcohol:avoid"), 900, 960, CTX)
    assert warn.satisfied and warn.code == "ALCOHOL_SERVED"
    assert candidate_allowed(resto, make_constraints(["alcohol:avoid"]), CTX)[0]
    assert not candidate_allowed(bar, make_constraints(["alcohol:avoid"]), CTX)[0]


def test_setting_constraints():
    indoors = con("setting:indoors_only")
    outdoors = con("setting:outdoors_only")
    assert check_constraint(make_candidate("fx:i", "museum", indoor=True), indoors, 900, 960, CTX).satisfied
    unknown = check_constraint(make_candidate("fx:n", "museum", indoor=None), indoors, 900, 960, CTX)
    assert not unknown.satisfied and unknown.code == "SETTING_CONFLICT"
    park = make_candidate("fx:p", "park")
    assert check_constraint(park, indoors, 900, 960, CTX).code == "SETTING_CONFLICT"
    assert check_constraint(park, outdoors, 900, 960, CTX).satisfied
    assert check_constraint(make_candidate("fx:c", "cafe"), outdoors, 900, 960, CTX).code == "SETTING_CONFLICT"


# --- must include / avoid --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "yes", "no"),
    [
        ("sit_down_meal", make_candidate("fx:r", "restaurant"), make_candidate("fx:c", "cafe")),
        ("outdoor_walk", make_candidate("fx:p", "park"), make_candidate("fx:m", "mall")),
        ("live_music", make_candidate("fx:lm", "live_music_venue"), make_candidate("fx:c", "cafe")),
        ("live_music", make_candidate("fx:cm", "cafe", live_music=True), make_candidate("fx:b", "bookstore")),
        ("live_music", make_candidate("ev:g", "event", tags=["music"]), make_candidate("ev:t", "event", tags=["talk"])),
        ("coffee", make_candidate("fx:c", "cafe"), make_candidate("fx:d", "dessert")),
        ("museum", make_candidate("fx:mu", "museum"), make_candidate("fx:g", "art_gallery")),
        ("art", make_candidate("fx:g", "art_gallery"), make_candidate("fx:p", "park")),
        ("books", make_candidate("fx:b", "bookstore"), make_candidate("fx:c", "cafe")),
        ("park", make_candidate("fx:l", "lake"), make_candidate("fx:v", "viewpoint")),
        ("event", make_candidate("ev:e", "event"), make_candidate("fx:c", "cafe")),
        ("shopping", make_candidate("fx:mk", "market"), make_candidate("fx:c", "cafe")),
        ("dessert", make_candidate("fx:d", "dessert"), make_candidate("fx:c", "cafe")),
    ],
)
def test_satisfies_include(value, yes, no):
    assert satisfies_include(yes, value)
    assert not satisfies_include(no, value)


def test_outdoor_walk_needs_outdoor_flag():
    assert not satisfies_include(make_candidate("fx:p", "park", indoor=None), "outdoor_walk")


def test_must_avoid_by_category_group_and_alcohol():
    assert matches_avoid(make_candidate("fx:m", "mall"), "mall")
    assert matches_avoid(make_candidate("fx:b", "bookstore"), "shopping")  # group
    assert matches_avoid(make_candidate("fx:br", "brewery"), "alcohol")
    assert matches_avoid(make_candidate("fx:r", "restaurant", serves_alcohol=True), "alcohol")
    assert not matches_avoid(make_candidate("fx:c", "cafe"), "mall")
    chk = check_constraint(make_candidate("fx:m", "mall"), con("must_avoid:mall"), 900, 960, CTX)
    assert not chk.satisfied and chk.code == "MUST_AVOID_VIOLATED"


def test_must_include_is_plan_level():
    assert check_constraint(make_candidate("fx:c", "cafe"), con("must_include:museum"), 900, 960, CTX).satisfied


# --- prefilter -------------------------------------------------------------------------------------


def test_candidate_allowed_excludes_known_and_unverified_violations():
    cons = make_constraints(["diet:vegetarian", "access:wheelchair"])
    assert candidate_allowed(make_candidate("fx:ok", "restaurant"), cons, CTX) == (True, [])
    nonveg = candidate_allowed(make_candidate("fx:nv", "restaurant", diet={"vegetarian": Evidence.NO}), cons, CTX)
    assert nonveg[0] is False and nonveg[1]
    assert not candidate_allowed(make_candidate("fx:u", "restaurant", diet={}), cons, CTX)[0]
    assert not candidate_allowed(make_candidate("fx:w", "park", wheelchair=Evidence.UNKNOWN), cons, CTX)[0]
    # diet doesn't apply to a park
    assert candidate_allowed(make_candidate("fx:p", "park", diet={}), cons, CTX)[0]
    outdoor = candidate_allowed(make_candidate("fx:p", "park"), make_constraints(["setting:indoors_only"]), CTX)
    assert outdoor[0] is False
    for reason in nonveg[1]:
        assert not any(ch.isdigit() for ch in reason)


def test_event_date_is_not_a_constraint_concern():
    ev = make_candidate("ev:e", "event", event_date=TARGET_DATE)
    assert candidate_allowed(ev, [], CTX) == (True, [])
