"""Integrity of the frozen world: schema, categories, geography, labelling, and the eval-critical inventory.

If a fixture edit breaks an eval case's premise (e.g. the assignment cluster stops being feasible, or a decoy
loses the property that makes it a decoy), these tests fail before any model eval runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import pytest

from app.domain.categories import category, is_category
from app.domain.schemas import Candidate, Evidence
from app.providers.fixture import FIXTURES_DIR, load_world
from app.providers.normalize import haversine_m, is_open_at, popularity_from_rating_count

WORLD = load_world()
RAW = json.loads((FIXTURES_DIR / "world.json").read_text(encoding="utf-8"))
BLR = WORLD.candidates["fixture:bangalore"]
BOM = WORLD.candidates["fixture:mumbai"]
BY_ID = {c.id: c for c in WORLD.all_candidates()}
CUBBON = (12.9763, 77.5929)


def _open_through(c: Candidate, start: int, end: int) -> bool:
    return c.hours.status == "known" and any(s <= start and end <= e for s, e in c.hours.periods)


def _likely_busy(c: Candidate, start: int, end: int) -> bool:
    return c.popularity == "high" and any(s < end and start < e for s, e in c.busy_windows)


def _km(c: Candidate, lat: float, lng: float) -> float:
    return haversine_m(c.lat, c.lng, lat, lng) / 1000


def _veg(c: Candidate, key: str = "vegetarian") -> Evidence:
    return c.diet.get(key, Evidence.UNKNOWN)


def _find(pred: Callable[[Candidate], bool], pool: list[Candidate] = BLR) -> list[Candidate]:
    return [c for c in pool if pred(c)]


# ---------------------------------------------------------------------------
# Structural integrity
# ---------------------------------------------------------------------------


def test_counts() -> None:
    assert 40 <= len(BLR) <= 60
    assert 10 <= len(BOM) <= 20
    assert WORLD.candidates["fixture:jaipur"] == [] and WORLD.candidates["fixture:london"] == []


def test_every_candidate_validates_against_the_schema() -> None:
    for c in WORLD.all_candidates():
        assert Candidate.model_validate(c.model_dump()) == c


def test_ids_unique_and_prefixed() -> None:
    ids = [c["id"] for c in RAW["candidates"]]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("fx:") for i in ids)


@pytest.mark.parametrize("cand", WORLD.all_candidates(), ids=lambda c: c.id)
def test_candidate_fields(cand: Candidate) -> None:
    city = WORLD.locations[cand.city_id]
    assert is_category(cand.category)
    assert cand.kind == "place"
    assert _km(cand, city.lat, city.lng) <= 40, "outside 40 km of its city"
    assert cand.source.provider == "fixture" and cand.source.attribution == "Synthetic test attributes"
    assert cand.cost.currency == city.currency and 0 <= cand.cost.min <= cand.cost.max
    assert cand.duration_range == category(cand.category).duration
    assert cand.maps_url is not None and cand.maps_url.startswith("https://www.google.com/maps/search/")
    for start, end in cand.hours.periods:
        assert 0 <= start < end <= 2880
    if cand.rating_count is not None:
        assert cand.popularity == popularity_from_rating_count(cand.rating_count)
    for s, e in cand.busy_windows:
        assert s < e
    assert set(cand.diet) <= {"vegetarian", "vegan", "jain"}
    if _veg(cand, "vegan") == Evidence.CONFIRMED or _veg(cand, "jain") == Evidence.CONFIRMED:
        assert _veg(cand) == Evidence.CONFIRMED
    if category(cand.category).group == "food":
        assert "vegetarian" in cand.diet, "food candidates state their vegetarian evidence explicitly"


def test_locations() -> None:
    blr = WORLD.locations["fixture:bangalore"]
    assert (blr.lat, blr.lng, blr.timezone, blr.currency) == (12.9716, 77.5946, "Asia/Kolkata", "INR")
    assert WORLD.locations["fixture:london"].currency == "GBP"
    for alias in ("bangalore", "bengaluru", "blr", "bangalore city"):
        assert WORLD.aliases[alias] == "fixture:bangalore"
    assert WORLD.aliases["bombay"] == "fixture:mumbai"


# ---------------------------------------------------------------------------
# Eval-critical inventory (eval_cases.json)
# ---------------------------------------------------------------------------


def test_assignment_example_cluster_is_feasible() -> None:
    """Low energy, vegetarian, avoid crowds, INR 2000, 15:00-19:00: a park walk, a veg cafe and a seated
    music venue, all close together near Cubbon Park."""
    park, cafe, music = BY_ID["fx:blr-cubbon-park"], BY_ID["fx:blr-leafy-lane-cafe"], BY_ID["fx:blr-lavelle-music-room"]
    slots = [(park, 900, 930), (cafe, 950, 1010), (music, 1030, 1120)]
    for c, start, end in slots:
        assert _open_through(c, start, end), c.id
        assert not _likely_busy(c, start, end), c.id
    assert park.category == "park" and park.indoor is False and 930 <= 1070  # walk ends before sunset
    assert _veg(cafe) == Evidence.CONFIRMED and cafe.seated and cafe.popularity == "low"
    assert music.seated and music.serves_alcohol is False and music.live_music
    for a in (park, cafe, music):
        for b in (park, cafe, music):
            assert haversine_m(a.lat, a.lng, b.lat, b.lng) <= 1500
    assert sum(c.cost.max for c in (park, cafe, music)) * 1.1 <= 2000 - 400  # room for fares and contingency


def test_veg_food_open_in_the_afternoon_near_cubbon() -> None:
    options = _find(lambda c: category(c.category).group == "food" and _veg(c) == Evidence.CONFIRMED
                    and c.popularity != "high" and _open_through(c, 900, 1140) and _km(c, *CUBBON) <= 1.5)
    assert len(options) >= 2


DECOYS: list[tuple[str, Callable[[Candidate], bool]]] = [
    ("fx:blr-mg-road-brewing", lambda c: c.category == "brewery" and c.serves_alcohol is True
     and c.popularity == "high" and _likely_busy(c, 900, 1140)),
    ("fx:blr-toit", lambda c: c.category == "brewery" and c.popularity == "high"),
    ("fx:blr-residency-kebab-house", lambda c: _veg(c) == Evidence.NO),
    ("fx:blr-matteo-coffea", lambda c: c.category == "cafe" and _veg(c) == Evidence.UNKNOWN),
    ("fx:blr-postal-heritage-museum", lambda c: c.category == "museum" and c.hours.status == "known"
     and c.hours.periods == []),
    ("fx:blr-rangoli-metro-art", lambda c: c.wheelchair == Evidence.UNKNOWN and c.cost.max == 0),
    ("fx:blr-ulsoor-lake", lambda c: c.category == "lake" and c.indoor is False),
    ("fx:blr-kasturba-tiffin-room", lambda c: _veg(c) == Evidence.CONFIRMED
     and max(e for _, e in c.hours.periods) == 900),
    ("fx:blr-rameshwaram-cafe", lambda c: _veg(c) == Evidence.CONFIRMED and _likely_busy(c, 900, 1140)),
    ("fx:blr-third-wave-indiranagar", lambda c: _veg(c, "vegan") == Evidence.UNKNOWN),
    ("fx:blr-chowdiah-hall", lambda c: c.hours.status == "unknown"),
    ("fx:blr-corner-house", lambda c: _likely_busy(c, 1000, 1030) and _veg(c) == Evidence.UNKNOWN),
]


@pytest.mark.parametrize(("cid", "check"), DECOYS, ids=[d[0] for d in DECOYS])
def test_decoys_keep_their_decoy_property(cid: str, check: Callable[[Candidate], bool]) -> None:
    assert check(BY_ID[cid])


def test_closed_at_three_decoy_closes_exactly_at_window_start() -> None:
    assert not is_open_at(BY_ID["fx:blr-kasturba-tiffin-room"].hours, 900)


def test_indiranagar_vegan_and_books_cluster() -> None:
    def indira(c: Candidate) -> bool:
        return c.neighbourhood == "Indiranagar"
    vegan = _find(lambda c: indira(c) and c.category == "cafe" and _veg(c, "vegan") == Evidence.CONFIRMED
                  and _open_through(c, 960, 1080) and c.popularity != "high")
    books = _find(lambda c: indira(c) and c.category == "bookstore" and _open_through(c, 960, 1200))
    assert vegan and books
    assert haversine_m(vegan[0].lat, vegan[0].lng, books[0].lat, books[0].lng) <= 1200
    assert vegan[0].cost.max + books[0].cost.max <= 1500 / 1.1 - 300


def test_koramangala_brunch_and_art() -> None:
    def kora(c: Candidate) -> bool:
        return c.neighbourhood == "Koramangala"
    brunch = _find(lambda c: kora(c) and "brunch" in c.tags and _open_through(c, 660, 750)
                   and min(s for s, _ in c.hours.periods) <= 480)
    art = _find(lambda c: kora(c) and c.category == "art_gallery" and _open_through(c, 780, 900))
    assert brunch and art
    assert brunch[0].cost.max + art[0].cost.max <= 1200 / 1.1 - 300


def test_jain_confirmed_restaurant() -> None:
    jain = _find(lambda c: c.category == "restaurant" and _veg(c, "jain") == Evidence.CONFIRMED)
    assert jain and _open_through(jain[0], 1020, 1110) and _km(jain[0], *CUBBON) <= 2


def test_wheelchair_confirmed_set() -> None:
    def wc(c: Candidate) -> bool:
        return c.wheelchair == Evidence.CONFIRMED
    assert _find(lambda c: wc(c) and c.category == "museum" and _open_through(c, 900, 1080))
    assert _find(lambda c: wc(c) and c.category == "cafe" and _veg(c) == Evidence.CONFIRMED
                 and _open_through(c, 900, 1140))
    assert _find(lambda c: wc(c) and c.category in {"park", "garden"} and "paved paths" in c.tags
                 and _open_through(c, 900, 1050))
    # impossible_budget (veg + wheelchair + sit-down meal at INR 1) must be infeasible only because of budget
    meal = _find(lambda c: wc(c) and category(c.category).meal and _veg(c) == Evidence.CONFIRMED
                 and _open_through(c, 900, 1140))
    assert meal and min(c.cost.min for c in meal) > 1


def test_free_and_cheap_day() -> None:
    near = lambda c: _km(c, *CUBBON) <= 2.5  # noqa: E731
    assert _find(lambda c: near(c) and c.category == "art_gallery" and c.cost.max == 0 and _open_through(c, 900, 1000))
    assert _find(lambda c: near(c) and c.category == "park" and c.cost.max == 0)
    assert _find(lambda c: near(c) and c.category == "heritage_walk" and c.cost.max == 0)
    assert _find(lambda c: near(c) and c.category == "street_food" and c.cost.max <= 150)
    seated = _find(lambda c: near(c) and c.seated and c.cost.max <= 150 and _open_through(c, 960, 1080))
    assert seated  # low energy needs a seated stop; it must fit a INR 300 day
    free_art = BY_ID["fx:blr-rangoli-metro-art"]
    assert haversine_m(seated[0].lat, seated[0].lng, free_art.lat, free_art.lng) <= 500


def test_date_night_options() -> None:
    fine = _find(lambda c: c.category == "restaurant" and _veg(c) == Evidence.NO and c.cost.min >= 1000
                 and _open_through(c, 1170, 1290))
    jazz = _find(lambda c: c.category == "live_music_venue" and "jazz" in c.tags
                 and max(e for _, e in c.hours.periods) >= 1380)
    assert fine and jazz
    assert (fine[0].cost.max + jazz[0].cost.max) * 1.1 <= 3000 - 200


def test_late_night_is_sparse() -> None:
    open_late = _find(lambda c: _open_through(c, 1410, 1440))
    assert any(c.category == "dessert" and max(e for _, e in c.hours.periods) >= 1500
               and _veg(c) == Evidence.CONFIRMED for c in open_late)
    assert len(open_late) <= 5
    assert not any(category(c.category).walking for c in open_late)


def test_rainy_day_indoor_options() -> None:
    indoor = _find(lambda c: c.indoor is True and c.category in {"museum", "art_gallery", "bookstore", "mall"}
                   and _open_through(c, 900, 1020) and _km(c, *CUBBON) <= 3)
    assert len(indoor) >= 5


def test_cheap_live_music_and_family_options() -> None:
    assert _find(lambda c: c.category == "live_music_venue" and c.cost.max <= 200 and "open mic" in c.tags
                 and _open_through(c, 960, 1080) and c.serves_alcohol is False)
    assert _find(lambda c: c.category == "museum" and {"science", "family"} <= set(c.tags))
    assert _find(lambda c: c.category == "dessert" and "ice cream" in c.tags and _veg(c) == Evidence.CONFIRMED)


def test_mumbai_has_a_feasible_cluster() -> None:
    cafe = [c for c in BOM if c.category == "cafe" and _veg(c) == Evidence.CONFIRMED and c.popularity != "high"]
    park = [c for c in BOM if c.category == "park" and c.cost.max == 0 and _open_through(c, 900, 960)]
    music = [c for c in BOM if c.category == "live_music_venue" and _open_through(c, 1020, 1110)]
    assert cafe and park and music
    assert haversine_m(cafe[0].lat, cafe[0].lng, park[0].lat, park[0].lng) <= 2000


# ---------------------------------------------------------------------------
# Events and weather fixtures
# ---------------------------------------------------------------------------


def test_event_fixture_inventory() -> None:
    raw = json.loads((FIXTURES_DIR / "events.json").read_text(encoding="utf-8"))
    scenarios = raw["scenarios"]
    assert {"valid_same_day", "wrong_date", "no_source_url", "jsonld_name_with_injected_instructions"} <= set(scenarios)
    assert raw["default_scenarios"] == ["valid_same_day", "wrong_date", "no_source_url"]
    assert scenarios["valid_same_day"]["jsonld"]["startDate"].startswith("2026-11-07")
    assert scenarios["valid_same_day"]["page_url"].startswith("https://")
    assert scenarios["wrong_date"]["jsonld"]["startDate"].startswith("2026-11-08")
    assert scenarios["no_source_url"]["page_url"] is None and "url" not in scenarios["no_source_url"]["jsonld"]
    injected = scenarios["jsonld_name_with_injected_instructions"]["jsonld"]["name"]
    assert "SYSTEM:" in injected and len(injected) > 120


def test_weather_fixture_inventory() -> None:
    raw = json.loads((FIXTURES_DIR / "weather.json").read_text(encoding="utf-8"))
    assert date.fromisoformat(raw["date"]) == date(2026, 11, 7)
    assert raw["scenarios"]["unavailable"] is None
    for name in ("clear", "rain_85pct"):
        table = raw["scenarios"][name]
        assert set(table) == set(WORLD.locations)
    assert raw["scenarios"]["rain_85pct"]["fixture:bangalore"]["precip_prob_max"] == 85
    assert abs(raw["scenarios"]["clear"]["fixture:bangalore"]["sunset_min"] - (17 * 60 + 50)) <= 5
    assert abs(raw["scenarios"]["clear"]["fixture:mumbai"]["sunset_min"] - (18 * 60 + 5)) <= 5
