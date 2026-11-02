from app.domain.categories import category, currency_for_country, currency_symbol, load_categories
from app.domain.pacing import energy_from_mood, meal_windows_between, profile_for
from app.domain.schemas import Energy


def test_categories_load_and_are_consistent():
    cats = load_categories()
    assert {"cafe", "restaurant", "park", "museum", "live_music_venue", "event"} <= set(cats)
    for key, info in cats.items():
        lo, default, hi = info.duration
        assert 0 < lo <= default <= hi, key
        assert info.cost_inr[0] <= info.cost_inr[1], key
        for start, end in info.busy:
            assert start < end, key
    assert category("bar").alcohol is True
    assert category("park").walking is True and category("park").indoor is False


def test_currency():
    assert currency_for_country("IN") == "INR"
    assert currency_for_country("gb") == "GBP"
    assert currency_for_country("ZZ") is None
    assert currency_symbol("INR") == "₹"


def test_energy_and_pacing():
    assert energy_from_mood("tired but wants to do something fun") == Energy.LOW
    assert energy_from_mood("full of energy, want to pack a lot in") == Energy.HIGH
    assert energy_from_mood("feeling okay") == Energy.MEDIUM
    assert profile_for(Energy.LOW).max_stops == 3
    assert profile_for(Energy.LOW).min_seated_stops == 1
    names = [w.name for w in meal_windows_between(900, 1140)]
    assert names == ["snack"]
