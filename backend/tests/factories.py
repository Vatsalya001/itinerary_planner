"""Small builders for domain tests. Other agents may import these read-only.

All data here is synthetic (labelled "Synthetic test fixture"); coordinates sit around central Bangalore.

    from tests.factories import make_candidate, make_ctx, make_prefs, make_ledger, bangalore_like_world
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from app.domain.categories import category
from app.domain.constraints import map_constraint_text
from app.domain.pacing import meal_windows_between, profile_for
from app.domain.schemas import (
    Candidate,
    Constraint,
    CostRange,
    DraftStop,
    Energy,
    Evidence,
    Hours,
    Location,
    PlanDraft,
    Preferences,
    Source,
    Weather,
    WorldContext,
)

TARGET_DATE = date(2026, 11, 7)  # eval_cases.json fixed_context.target_saturday
NOW = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)  # 2026-11-05T10:00+05:30

# The same Locations as data/fixtures/world.json (what FixtureGeocoder returns), so domain tests and graph/eval
# tests see one frozen world: the Bangalore fixture is named "Bengaluru" and Mumbai is centred on Fort.
BANGALORE = Location(
    id="fixture:bangalore", name="Bengaluru", country_code="IN", admin1="Karnataka",
    lat=12.9716, lng=77.5946, timezone="Asia/Kolkata", currency="INR", population=8_443_675,
)
MUMBAI = Location(
    id="fixture:mumbai", name="Mumbai", country_code="IN", admin1="Maharashtra",
    lat=18.9322, lng=72.8317, timezone="Asia/Kolkata", currency="INR", population=12_691_836,
)
LONDON = Location(
    id="fixture:london", name="London", country_code="GB", admin1="England", lat=51.5074, lng=-0.1278,
    timezone="Europe/London", currency="GBP", population=8_961_989,
)

ALL_DAY = Hours(status="known", periods=[(8 * 60, 23 * 60)])


def make_candidate(id: str = "fx:cafe", category_key: str = "cafe", *, lat: float | None = None,
                   lng: float | None = None, offset_km: tuple[float, float] = (0.0, 0.0),
                   cost: int | tuple[int, int] | None = None, **overrides: Any) -> Candidate:
    """A synthetic candidate whose defaults come from the category (indoor, seated, effort, tags, duration,
    category cost band). Defaults: open 08:00-23:00, confirmed vegetarian and wheelchair access, medium
    popularity, fixture source. ``offset_km`` is (north, east) from central Bangalore."""
    info = category(category_key)
    base_lat = BANGALORE.lat + offset_km[0] / 111.0
    base_lng = BANGALORE.lng + offset_km[1] / 108.5
    if cost is None:
        lo, hi = info.cost_inr
    elif isinstance(cost, int):
        lo = hi = cost
    else:
        lo, hi = cost
    fields: dict[str, Any] = {
        "id": id,
        "kind": "event" if category_key == "event" else "place",
        "name": overrides.pop("name", id.split(":")[-1].replace("_", " ").title()),
        "category": category_key,
        "city_id": BANGALORE.id,
        "lat": base_lat if lat is None else lat,
        "lng": base_lng if lng is None else lng,
        "cost": CostRange(min=lo, max=hi, currency="INR",
                          basis="free" if hi == 0 else "category_estimate"),
        "rating": 4.4,
        "rating_count": 800,
        "hours": ALL_DAY,
        "diet": {"vegetarian": Evidence.CONFIRMED},
        "wheelchair": Evidence.CONFIRMED,
        "indoor": info.indoor,
        "seated": info.seated,
        "effort": info.effort,
        "serves_alcohol": info.alcohol,
        "popularity": "medium",
        "busy_windows": [tuple(w) for w in info.busy],
        "tags": list(info.tags),
        "duration_range": tuple(info.duration),
        "source": Source(provider="fixture", attribution="Synthetic test fixture"),
    }
    if category_key == "event":
        fields.update({
            "event_date": TARGET_DATE, "event_start_min": 17 * 60, "event_end_min": 19 * 60,
            "hours": Hours(),
            "source": Source(provider="fixture_event", url="https://example.org/events/1",
                             attribution="Synthetic test fixture"),
        })
    fields.update(overrides)
    return Candidate(**fields)


def make_weather(kind: str = "clear", sunset_min: int | None = 17 * 60 + 50) -> Weather:
    if kind == "unavailable":
        return Weather(status="unavailable")
    if kind in {"rain", "rain_85pct"}:
        return Weather(status="ok", summary="Rain likely", precip_prob_max=85, temp_max_c=24.0,
                       temp_min_c=19.0, weather_code=63, sunrise_min=6 * 60 + 10, sunset_min=sunset_min)
    return Weather(status="ok", summary="Partly cloudy", precip_prob_max=10, temp_max_c=28.0, temp_min_c=19.0,
                   weather_code=2, sunrise_min=6 * 60 + 10, sunset_min=sunset_min)


def make_ctx(*, start_min: int = 900, available: int = 240, energy: Energy | str = Energy.LOW,
             weather: str | Weather = "clear", sunset_min: int | None = 17 * 60 + 50,
             location: Location = BANGALORE, day: date = TARGET_DATE, end_min: int | None = None) -> WorldContext:
    energy = Energy(energy)
    end = start_min + available if end_min is None else end_min
    wx = weather if isinstance(weather, Weather) else make_weather(weather, sunset_min)
    return WorldContext(
        location=location, date=day, start_min=start_min, end_min=end, weather=wx,
        pacing=profile_for(energy), meal_windows=meal_windows_between(start_min, end), now=NOW,
    )


def make_constraints(items: Iterable[str | Constraint]) -> list[Constraint]:
    """Accepts "kind:value" keys, free text ("vegetarian", "avoid crowded places") or Constraint objects."""
    out: list[Constraint] = []
    for item in items:
        if isinstance(item, Constraint):
            found = [item]
        elif ":" in item and item.split(":", 1)[0] in {
            "diet", "alcohol", "access", "crowd", "setting", "must_include", "must_avoid",
        }:
            kind, value = item.split(":", 1)
            found = [Constraint(kind=kind, value=value, source_text=value.replace("_", " "))]
        else:
            found = map_constraint_text(item)
            if not found:
                raise ValueError(f"constraint text does not map: {item!r}")
        for c in found:
            if c not in out:
                out.append(c)
    return out


def make_prefs(*, budget: int | None = 2000, available: int = 240, start_min: int = 900,
               energy: Energy | str = Energy.LOW, constraints: Iterable[str | Constraint] = (),
               interests: Sequence[str] = ("food", "music", "walks"), currency: str | None = "INR",
               neighbourhood: str | None = None, city: str = "Bangalore", **overrides: Any) -> Preferences:
    fields: dict[str, Any] = {
        "city": city, "budget": budget, "currency": currency, "start_min": start_min,
        "available_minutes": available, "date": TARGET_DATE, "mood": "tired but wants to do something fun",
        "energy": Energy(energy), "interests": list(interests), "hard_constraints": make_constraints(constraints),
        "neighbourhood": neighbourhood,
    }
    fields.update(overrides)
    return Preferences(**fields)


def make_ledger(*candidates: Candidate) -> dict[str, Candidate]:
    return {c.id: c for c in candidates}


def make_draft(*stops: tuple[str, int] | DraftStop, summary_note: str = "") -> PlanDraft:
    """make_draft(("fx:cafe", 45), ("fx:park", 40)) -> PlanDraft with neutral why text."""
    built = [
        s if isinstance(s, DraftStop) else DraftStop(candidate_id=s[0], duration_min=s[1], why="A good fit for you.")
        for s in stops
    ]
    return PlanDraft(stops=built, summary_note=summary_note)


def bangalore_like_world() -> list[Candidate]:
    """A synthetic Bangalore-like set: a feasible low-effort vegetarian cluster plus decoys (likely busy,
    non-vegetarian, closed at 15:00, unknown diet, unknown accessibility, outdoor-only, alcohol venue)."""
    U, N = Evidence.UNKNOWN, Evidence.NO
    return [
        # feasible cluster (central, within walking/short auto distance)
        make_candidate("fx:quiet_cafe", "cafe", offset_km=(0.0, 0.0), cost=(150, 350), rating=4.6,
                       popularity="low", tags=["coffee", "food", "relax", "books"]),
        make_candidate("fx:veg_thali", "restaurant", offset_km=(0.6, 0.4), cost=(250, 500), rating=4.5,
                       popularity="medium", busy_windows=[(750, 870)]),
        make_candidate("fx:cubbon_walk", "park", offset_km=(0.5, -0.3), cost=0, popularity="medium",
                       busy_windows=[(360, 540)], hours=Hours(status="known", periods=[(360, 1080)])),
        make_candidate("fx:jazz_room", "live_music_venue", offset_km=(1.0, 1.2), cost=(300, 600), rating=4.5,
                       live_music=True, popularity="medium", busy_windows=[(1200, 1380)],
                       hours=Hours(status="known", periods=[(960, 1380)])),
        make_candidate("fx:book_nook", "bookstore", offset_km=(0.3, 0.5), cost=(0, 300), popularity="low"),
        make_candidate("fx:sweet_spot", "dessert", offset_km=(0.8, 0.2), cost=(100, 250), popularity="low"),
        make_candidate("fx:art_house", "art_gallery", offset_km=(1.5, 0.8), cost=(0, 100), popularity="low"),
        # decoys
        make_candidate("fx:busy_brewpub", "brewery", offset_km=(1.0, 0.0), popularity="high",
                       busy_windows=[(780, 900), (1140, 1440)], serves_alcohol=True),
        make_candidate("fx:packed_cafe", "cafe", offset_km=(0.1, 0.1), rating=4.8, popularity="high",
                       busy_windows=[(660, 780), (900, 1200)], tags=["coffee", "food"]),
        make_candidate("fx:kebab_corner", "restaurant", offset_km=(0.4, 0.4), rating=4.7,
                       diet={"vegetarian": N}),
        make_candidate("fx:late_opener", "restaurant", offset_km=(0.2, 0.6), rating=4.6,
                       hours=Hours(status="known", periods=[(1140, 1380)])),
        make_candidate("fx:mystery_diner", "restaurant", offset_km=(0.7, 0.7), rating=4.6, diet={"vegetarian": U}),
        make_candidate("fx:stairs_cafe", "cafe", offset_km=(0.6, 0.1), rating=4.7, wheelchair=U,
                       tags=["coffee", "food"]),
        make_candidate("fx:rooftop_walk", "viewpoint", offset_km=(2.0, 2.0), cost=(0, 100), indoor=False),
        make_candidate("fx:far_lake", "lake", offset_km=(9.0, 9.0), cost=0),
        make_candidate("fx:unknown_hours_museum", "museum", offset_km=(1.2, -0.6), cost=(20, 100),
                       hours=Hours(status="unknown"), diet={}),
        make_candidate("fx:mall", "mall", offset_km=(3.0, 1.0), popularity="high", busy_windows=[(780, 1290)]),
    ]


def synthetic_candidates(n: int = 60, seed: int = 7) -> list[Candidate]:
    """``n`` seeded synthetic candidates across categories within ~4 km of central Bangalore, with mixed
    ratings, popularity, diet and wheelchair evidence (for performance and robustness tests)."""
    import random

    rng = random.Random(seed)
    keys = ["cafe", "restaurant", "dessert", "park", "garden", "lake", "museum", "art_gallery", "bookstore",
            "live_music_venue", "street_food", "theatre", "viewpoint", "market", "heritage_walk"]
    out = []
    for i in range(n):
        key = keys[i % len(keys)]
        out.append(make_candidate(
            f"fx:gen{i}", key, offset_km=(rng.uniform(-4, 4), rng.uniform(-4, 4)),
            rating=round(rng.uniform(3.8, 4.8), 1),
            popularity=rng.choice(["low", "medium", "medium", "high"]),
            diet={"vegetarian": rng.choice([Evidence.CONFIRMED, Evidence.CONFIRMED, Evidence.UNKNOWN, Evidence.NO])},
            wheelchair=rng.choice([Evidence.CONFIRMED, Evidence.UNKNOWN]),
        ))
    return out
