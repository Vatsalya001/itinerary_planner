import pytest

from app.domain.costs import compute_totals, contingency, stop_cost, worst_case
from app.domain.schemas import CostRange, Leg
from tests.factories import make_candidate


@pytest.mark.parametrize(("upper", "expected"), [(0, 0), (1, 1), (9, 1), (10, 1), (11, 2), (1000, 100), (1001, 101)])
def test_contingency_is_ceil_ten_percent(upper, expected):
    assert contingency(upper) == expected


def test_worst_case():
    assert worst_case(1000, 0) == 1100
    assert worst_case(900, 101) == 1001 + 101


def test_stop_cost_is_candidate_cost_independent_of_duration():
    c = make_candidate("fx:c", "cafe", cost=(200, 400))
    assert stop_cost(c) == c.cost


def test_compute_totals():
    a = make_candidate("fx:a", "cafe", cost=(200, 400))
    b = make_candidate("fx:b", "restaurant", cost=(300, 600))
    leg = Leg(from_id="fx:a", to_id="fx:b", mode="auto", distance_km=3.0, minutes=15, minutes_max=20,
              fare=CostRange(min=54, max=68, currency="INR", basis="category_estimate"))
    t = compute_totals([a, b], [leg], currency="INR", budget=1200, elapsed_min=200, available_minutes=240,
                       walking_min=0)
    assert (t.stops_cost_min, t.stops_cost_max, t.legs_cost_min, t.legs_cost_max) == (500, 1000, 54, 68)
    assert t.contingency == 107  # ceil(106.8)
    assert t.worst_case == 1068 + 107
    assert t.budget_left == 1200 - t.worst_case
    assert t.minutes_left == 40
    none_budget = compute_totals([a], [], currency="INR", budget=None, elapsed_min=50, available_minutes=240,
                                 walking_min=0)
    assert none_budget.budget_left is None
