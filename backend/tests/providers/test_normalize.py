from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.categories import category, is_category, load_categories
from app.domain.schemas import Candidate, CostRange, Hours, PlaceQuery, Source, WorldContext
from app.providers import normalize as nz
from app.providers.normalize import (
    ALWAYS_OPEN,
    busy_windows_for,
    category_default_cost,
    clean_text,
    google_types_to_category,
    is_open_at,
    maps_search_url,
    parse_osm_opening_hours,
    popularity_from_rating_count,
    price_level_band,
    search_local,
    weather_summary,
)

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("primary", "types", "expected"),
    [
        ("cafe", ["cafe", "restaurant", "food"], "cafe"),
        ("coffee_shop", [], "cafe"),
        ("restaurant", ["restaurant"], "restaurant"),
        ("indian_restaurant", ["indian_restaurant", "restaurant"], "restaurant"),
        ("vegan_restaurant", [], "restaurant"),
        ("fine_dining_restaurant", [], "restaurant"),
        ("ice_cream_shop", ["dessert_shop"], "dessert"),
        ("fast_food_restaurant", [], "street_food"),
        ("bar", ["bar", "restaurant"], "bar"),
        ("brewery", [], "brewery"),
        ("live_music_venue", [], "live_music_venue"),
        ("performing_arts_theater", [], "theatre"),
        ("museum", [], "museum"),
        ("art_gallery", [], "art_gallery"),
        ("book_store", [], "bookstore"),
        ("botanical_garden", [], "garden"),
        ("park", [], "park"),
        ("hindu_temple", [], "place_of_worship"),
        ("shopping_mall", [], "mall"),
        (None, ["point_of_interest", "establishment", "cafe"], "cafe"),
        ("point_of_interest", ["establishment"], None),
        (None, None, None),
    ],
)
def test_google_types_to_category(primary: str | None, types: list[str] | None, expected: str | None) -> None:
    assert google_types_to_category(primary, types) == expected


def test_primary_type_takes_precedence() -> None:
    assert google_types_to_category("bar", ["restaurant", "cafe"]) == "bar"
    assert nz.google_types_to_categories("restaurant", ["cafe", "bar"]) == ["restaurant", "cafe", "bar"]


def test_every_google_mapping_targets_a_real_category() -> None:
    for target in nz._GOOGLE_TYPE_MAP.values():
        assert is_category(target), target


def test_categories_for_filter_accepts_keys_and_groups() -> None:
    assert nz.categories_for_filter("cafe") == {"cafe"}
    assert nz.categories_for_filter("outdoors") == {"park", "garden", "lake", "heritage_walk", "viewpoint"}
    assert nz.categories_for_filter("nonsense") is None
    assert nz.categories_for_filter(None) is None


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------


def test_price_level_band_inr() -> None:
    band = price_level_band("PRICE_LEVEL_MODERATE", "INR")
    assert band == CostRange(min=400, max=1000, currency="INR", basis="price_level_estimate")
    assert price_level_band(0, "INR") == CostRange(min=0, max=0, currency="INR", basis="price_level_estimate")
    assert price_level_band("PRICE_LEVEL_UNSPECIFIED", "INR") is None
    assert price_level_band(None, "INR") is None


def test_price_level_band_other_currencies() -> None:
    gbp = price_level_band("PRICE_LEVEL_EXPENSIVE", "GBP")
    assert gbp is not None and (gbp.min, gbp.max, gbp.currency) == (35, 70, "GBP")
    unknown = price_level_band("PRICE_LEVEL_INEXPENSIVE", "THB")
    assert unknown is not None and unknown.currency == "USD"  # never priced in a currency we have no band for


def test_price_bands_are_monotonic_and_cover_all_categories() -> None:
    raw = nz.load_price_bands()
    assert raw["estimate"] is True
    for currency, bands in raw["bands"].items():
        previous_max = -1
        for level in nz.PRICE_LEVELS:
            lo, hi = bands[level]
            assert lo <= hi, (currency, level)
            assert hi >= previous_max, (currency, level)
            previous_max = hi
    assert set(raw["category_levels"]) == set(load_categories())


def test_category_default_cost() -> None:
    cafe = category_default_cost("cafe", "INR")
    assert (cafe.min, cafe.max, cafe.basis) == (*category("cafe").cost_inr, "category_estimate")
    park = category_default_cost("park", "INR")
    assert (park.min, park.max, park.basis) == (0, 0, "free")
    restaurant_gbp = category_default_cost("restaurant", "GBP")
    assert (restaurant_gbp.min, restaurant_gbp.max, restaurant_gbp.currency) == (5, 35, "GBP")
    for key in load_categories():
        for currency in ("INR", "GBP", "USD", "EUR"):
            cost = category_default_cost(key, currency)
            assert cost.min <= cost.max and cost.currency == currency


# ---------------------------------------------------------------------------
# Popularity and busy windows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [(5000, "high"), (25000, "high"), (4999, "medium"), (800, "medium"), (799, "low"), (0, "low"), (None, "medium")],
)
def test_popularity_from_rating_count(count: int | None, expected: str) -> None:
    assert popularity_from_rating_count(count) == expected


def test_busy_windows_for() -> None:
    assert busy_windows_for("cafe", "low") == []
    assert busy_windows_for("cafe", "high") == [tuple(w) for w in category("cafe").busy]
    assert busy_windows_for("art_gallery", "high") == []


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_maps_search_url_with_place_id() -> None:
    url = maps_search_url("Café & Books", 12.97, 77.59, "ChIJabc123")
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "www.google.com" and parts.path == "/maps/search/"
    q = parse_qs(parts.query)
    assert q == {"api": ["1"], "query": ["Café & Books"], "query_place_id": ["ChIJabc123"]}


def test_maps_search_url_without_place_id_uses_coordinates() -> None:
    q = parse_qs(urlsplit(maps_search_url("Somewhere", 12.9716, 77.5946)).query)
    assert q == {"api": ["1"], "query": ["12.971600,77.594600"]}


def test_is_https_url() -> None:
    assert nz.is_https_url("https://example.com/x")
    assert not nz.is_https_url("http://example.com/x")
    assert not nz.is_https_url("javascript:alert(1)")
    assert not nz.is_https_url(None)


# ---------------------------------------------------------------------------
# OSM opening hours (weekday: 0 = Monday ... 5 = Saturday, 6 = Sunday)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "weekday", "periods"),
    [
        ("Mo-Su 06:00-18:00", 5, [(360, 1080)]),
        ("Mo-Fr 09:00-17:00; Sa 10:00-14:00", 5, [(600, 840)]),
        ("Mo-Fr 09:00-17:00; Sa 10:00-14:00", 6, []),  # Sunday not listed -> closed
        ("Mo-Fr 09:00-17:00; Sa 10:00-14:00", 2, [(540, 1020)]),
        ("24/7", 5, [ALWAYS_OPEN]),
        ("off", 5, []),
        ("closed", 3, []),
        ("Tu-Su 10:00-17:00", 0, []),
        ("Tu-Su 10:00-17:00", 5, [(600, 1020)]),
        ("Mo-Su 12:30-16:00, 19:00-23:00", 5, [(750, 960), (1140, 1380)]),
        ("Mo-Su 18:00-02:00", 5, [(0, 120), (1080, 1560)]),
        ("Fr 20:00-02:00", 5, [(0, 120)]),
        ("10:00-22:00", 2, [(600, 1320)]),
        ("7:30-21:00", 5, [(450, 1260)]),
        ("Mo-Sa 10:00-20:00; Su off", 6, []),
        ("Mo-Sa 10:00-20:00; Su off", 5, [(600, 1200)]),
        ("Mo-Su,PH 06:00-18:00", 5, [(360, 1080)]),
        ("Mo-Sa 10:00-20:00; PH off", 5, [(600, 1200)]),
        ("Fr-Mo 10:00-12:00", 6, [(600, 720)]),
        ("Fr-Mo 10:00-12:00", 2, []),
        ("Mo,We,Sa 09:00-12:00", 5, [(540, 720)]),
        ("Tu-Th 11:00-22:00; Fr-Su 11:00-23:00", 5, [(660, 1380)]),
        ("Mo-Su 00:00-24:00", 5, [(0, 2880)]),
        ("Mo-Sa 00:00-24:00; Su 10:00-12:00", 5, [(0, 1440)]),
        ("Fr-Sa 18:00-24:00; Su 00:00-02:00", 5, [(1080, 1560)]),
    ],
)
def test_parse_osm_opening_hours(text: str, weekday: int, periods: list[tuple[int, int]]) -> None:
    hours = parse_osm_opening_hours(text, weekday)
    assert hours.status == "known"
    assert hours.periods == periods


@pytest.mark.parametrize(
    "text",
    [None, "", "sunrise-sunset", "Jan-Mar Mo-Fr 08:00-12:00", "Mo-Fr", '"by appointment"', "Mo-Fr 9am-5pm",
     "Mo-Su 10:00-18:00 || \"call\"", "week 1-53/2 Fr 09:00-12:00", "Xx 10:00-12:00"],
)
def test_parse_osm_opening_hours_unknown(text: str | None) -> None:
    assert parse_osm_opening_hours(text, 5) == Hours(status="unknown")


def test_is_open_at() -> None:
    known = Hours(status="known", periods=[(600, 900), (1080, 1500)])
    assert is_open_at(known, 600) is True
    assert is_open_at(known, 899) is True
    assert is_open_at(known, 900) is False
    assert is_open_at(known, 1450) is True  # after midnight
    assert is_open_at(Hours(status="known", periods=[]), 700) is False
    assert is_open_at(Hours(status="unknown"), 700) is None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def test_weather_summary() -> None:
    assert weather_summary(63, 23.5, 85) == "Moderate rain, high 24°C, 85% chance of rain"
    assert weather_summary(1, None, None) == "Mainly clear"
    assert weather_summary(None, 20.0, 0) == "Weather, high 20°C, 0% chance of rain"
    assert nz.wmo_description(42) == "Mixed conditions"


def test_clean_text_strips_markup_and_controls_and_truncates() -> None:
    assert clean_text("  <b>Jazz</b>\u200b Night\x00\n ", 120) == "Jazz Night"
    assert clean_text("abcdef", 3) == "abc"
    assert clean_text("a\u202eb", 10) == "a b"


# ---------------------------------------------------------------------------
# search_local (direct)
# ---------------------------------------------------------------------------


def _cand(cid: str, name: str, cat: str, lat: float, lng: float, **kw: object) -> Candidate:
    data: dict[str, object] = {
        "id": cid, "name": name, "category": cat, "city_id": "x", "lat": lat, "lng": lng,
        "cost": CostRange(min=0, max=100, currency="INR", basis="category_estimate"),
        "tags": list(category(cat).tags), "source": Source(provider="fixture"),
    }
    data.update(kw)
    return Candidate.model_validate(data)


def test_search_local_filters_rank_and_isolate(blr_ctx: WorldContext) -> None:
    cands = [
        _cand("a", "Quiet Bean", "cafe", 12.972, 77.595, popularity="low"),
        _cand("b", "Busy Bean", "cafe", 12.9715, 77.5947, popularity="high"),
        _cand("c", "Green Park", "park", 12.973, 77.596),
        _cand("d", "Far Cafe", "cafe", 12.99, 77.70),  # ~11 km away
        _cand("e", "Mumbai Cafe", "cafe", 18.93, 72.83),  # other city
        _cand("f", "Closed Cafe", "cafe", 12.972, 77.595, hours=Hours(status="known", periods=[(600, 800)])),
    ]
    res = search_local(cands, PlaceQuery(intent="quiet cafe"), blr_ctx)
    assert [c.id for c in res] == ["a", "f", "b"]  # quiet ranks low popularity first; far and other city dropped
    assert all(c.city_id == "fixture:bangalore" for c in res)
    res = search_local(cands, PlaceQuery(intent="quiet cafe", open_at_min=900), blr_ctx)
    assert "f" not in [c.id for c in res]
    res = search_local(cands, PlaceQuery(intent="cafe", radius_m=20000), blr_ctx)
    assert "d" in [c.id for c in res] and "e" not in [c.id for c in res]
    assert search_local(cands, PlaceQuery(intent="sushi"), blr_ctx) == []
    assert [c.id for c in search_local(cands, PlaceQuery(intent="", category="outdoors"), blr_ctx)] == ["c"]


def test_search_local_neighbourhood_centres_the_search(blr_ctx: WorldContext) -> None:
    far = _cand("k", "Kora Cafe", "cafe", 12.9352, 77.6245)  # ~5.2 km from the centre
    hoods = {"koramangala": (12.9352, 77.6245)}
    assert search_local([far], PlaceQuery(intent="cafe"), blr_ctx, hoods) == []
    assert [c.id for c in search_local([far], PlaceQuery(intent="cafe in Koramangala"), blr_ctx, hoods)] == ["k"]


def test_search_local_ignores_noise_words(blr_ctx: WorldContext) -> None:
    cands = [_cand("a", "Bean", "cafe", 12.972, 77.595)]
    assert [c.id for c in search_local(cands, PlaceQuery(intent="a nice rooftop cafe this evening"), blr_ctx)] == ["a"]


# ---------------------------------------------------------------------------
# Verification fixes (D-26 https URLs, D-19 midnight join, neighbourhood centring)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://example.com/x", True),
        ("HTTPS://Example.com/x?q=1", True),
        ("https://example.com:8443/x", True),
        ("https://", False),
        ("https:///path-only", False),
        ("https://user@evil.example/x", False),
        ("https://good.example@evil.example/x", False),
        ("https://user:pw@good.example/", False),
        ("https://exa mple.com/", False),
        ("https://example.com/\nx", False),
        ("https://example.com:99999/", False),
        ("https://.example.com/", False),
        ("https:\\\\example.com", False),
        ("http://example.com/", False),
        ("javascript:alert(1)", False),
        ("https://" + "a" * 2050 + ".com", False),
        (None, False),
        (42, False),
    ],
)
def test_is_https_url_requires_a_real_host(url: object, ok: bool) -> None:
    assert nz.is_https_url(url) is ok


def test_join_next_day() -> None:
    assert nz.join_next_day([(600, 1440)], [(0, 1440)]) == [(600, 2880)]
    assert nz.join_next_day([(600, 1440)], [(60, 600)]) == [(600, 1440)]  # next day opens later: no join
    assert nz.join_next_day([(600, 1320)], [(0, 1440)]) == [(600, 1320)]  # closes before midnight
    assert nz.join_next_day([(1080, 1560)], [(0, 300)]) == [(1080, 1740)]
    assert nz.join_next_day([(600, 1440)], []) == [(600, 1440)]


def test_neighbourhood_centre(blr_ctx: WorldContext) -> None:
    loc = blr_ctx.location
    table = {"indiranagar": (12.9750, 77.6405)}
    assert nz.neighbourhood_centre("Indiranagar", loc, table) == (12.9750, 77.6405)
    assert nz.neighbourhood_centre("  INDIRANAGAR ", loc, table) == (12.9750, 77.6405)
    assert nz.neighbourhood_centre("Koramangala", loc) == (12.9352, 77.6245)  # gazetteer fallback
    assert nz.neighbourhood_centre("Bandra", loc) is None  # a Mumbai neighbourhood is not in Bengaluru
    assert nz.neighbourhood_centre("Atlantis Heights", loc) is None
    assert nz.neighbourhood_centre("", loc) is None and nz.neighbourhood_centre(None, loc) is None


def test_search_local_uses_query_neighbourhood(blr_ctx: WorldContext) -> None:
    far = _cand("k", "Kora Cafe", "cafe", 12.9352, 77.6245, neighbourhood="Koramangala")  # ~5.2 km out
    near = _cand("n", "Centre Cafe", "cafe", 12.9720, 77.5950)
    assert [c.id for c in search_local([far, near], PlaceQuery(intent="cafe"), blr_ctx)] == ["n"]
    res = search_local([far, near], PlaceQuery(intent="cafe", neighbourhood="Koramangala"), blr_ctx)
    assert [c.id for c in res] == ["k"]  # centred on the gazetteer centroid, the centre is now ~5 km away
    res = search_local([far, near], PlaceQuery(intent="cafe in koramangala", neighbourhood="Koramangala",
                                               radius_m=10000), blr_ctx)
    assert [c.id for c in res] == ["k", "n"]  # in-neighbourhood boost, the name is not a search word
    pinned = search_local([far, near], PlaceQuery(intent="cafe", neighbourhood="Koramangala",
                                                  near_lat=12.9720, near_lng=77.5950), blr_ctx)
    assert [c.id for c in pinned] == ["n"]  # an explicit near point wins
    unknown = search_local([far, near], PlaceQuery(intent="cafe", neighbourhood="Nowhere Nagar"), blr_ctx)
    assert [c.id for c in unknown] == ["n"]  # unknown neighbourhood: city centre
