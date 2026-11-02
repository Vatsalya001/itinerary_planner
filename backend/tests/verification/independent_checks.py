"""Independent plan checks for the Phase 1 verification suite.

Everything here recomputes facts from the raw data files (``data/fixtures/*.json``, ``data/snapshot/*.json``,
``data/categories.yaml``, ``data/cities.yaml``) and from the returned plan's own times, never from the
validator, the planner's flags or ``PlanResult.checks``. Only the pacing table is imported from
``app.domain.pacing`` (it is the spec, §5).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.domain.pacing import PROFILES
from app.domain.schemas import Energy, PlanResult

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
DATA = BACKEND / "data"
TARGET = date(2026, 11, 7)
CITY_RADIUS_KM = 40.0
DETOUR = 1.4
WALK_MAX_KM = 1.2
EARTH_KM = 6371.0088


@lru_cache(maxsize=1)
def spec() -> dict[str, Any]:
    return json.loads((REPO / "eval_cases.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def world() -> dict[str, Any]:
    return json.loads((DATA / "fixtures" / "world.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def weather_fixture() -> dict[str, Any]:
    return json.loads((DATA / "fixtures" / "weather.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def events_fixture() -> dict[str, Any]:
    return json.loads((DATA / "fixtures" / "events.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def snapshot() -> dict[str, Any]:
    return json.loads((DATA / "snapshot" / "bangalore.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def cats() -> dict[str, Any]:
    return yaml.safe_load((DATA / "categories.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def cities() -> dict[str, Any]:
    return yaml.safe_load((DATA / "cities.yaml").read_text(encoding="utf-8"))


def location(loc_id: str) -> dict[str, Any]:
    return next(loc for loc in world()["locations"] if loc["id"] == loc_id)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


# ---------------------------------------------------------------------------------------------------
# Ground truth per candidate ID
# ---------------------------------------------------------------------------------------------------


@dataclass
class Truth:
    id: str
    name: str
    category: str
    lat: float
    lng: float
    cost_min: int
    cost_max: int
    hours: list[tuple[int, int]] | None  # None = unknown
    diet: dict[str, str]
    wheelchair: str
    indoor: bool
    seated: bool
    walking: bool
    popularity: str
    busy: list[tuple[int, int]]
    is_event: bool = False
    event_window: tuple[int, int] | None = None
    event_date: date | None = None
    source_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _fixture_truth(raw: dict[str, Any]) -> Truth:
    info = cats()[raw["category"]]
    cost = raw.get("cost") or [*info["cost_inr"], "category_estimate"]
    hours = raw.get("hours")
    popularity = raw.get("popularity", "medium")
    if "busy_windows" in raw:
        busy = [(int(w[0]), int(w[1])) for w in raw["busy_windows"]]
    else:
        busy = [] if popularity == "low" else [(int(w[0]), int(w[1])) for w in info["busy"]]
    diet = {k: v for k, v in (raw.get("diet") or {}).items()}
    return Truth(
        id=raw["id"], name=raw["name"], category=raw["category"], lat=raw["lat"], lng=raw["lng"],
        cost_min=cost[0], cost_max=cost[1], hours=None if hours is None else [(int(p[0]), int(p[1])) for p in hours],
        diet=diet, wheelchair=raw.get("wheelchair", "unknown"),
        indoor=raw.get("indoor", info["indoor"]), seated=raw.get("seated", info["seated"]),
        walking=info["walking"], popularity=popularity, busy=busy,
    )


_OSM_DAYS = ["mo", "tu", "we", "th", "fr", "sa", "su"]


def _osm_hours_saturday(text: str | None) -> list[tuple[int, int]] | None:
    """Tiny OSM opening_hours reader for the simple forms used in the snapshot (Saturday only)."""
    if not text:
        return None
    sat = 5
    result: list[tuple[int, int]] | None = None
    for rule in text.split(";"):
        rule = rule.strip()
        m = re.match(r"^([A-Za-z]{2}(?:-[A-Za-z]{2})?(?:,[A-Za-z]{2}(?:-[A-Za-z]{2})?)*)\s+(.*)$", rule)
        if m:
            days: set[int] = set()
            for part in m.group(1).lower().split(","):
                if "-" in part:
                    a, b = part.split("-")
                    i, j = _OSM_DAYS.index(a), _OSM_DAYS.index(b)
                    k = i
                    while True:
                        days.add(k)
                        if k == j:
                            break
                        k = (k + 1) % 7
                else:
                    days.add(_OSM_DAYS.index(part))
            rest = m.group(2)
        else:
            days, rest = set(range(7)), rule
        periods = []
        for rng in rest.split(","):
            mm = re.fullmatch(r"\s*(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})\s*", rng)
            if not mm:
                return None
            o = int(mm[1]) * 60 + int(mm[2])
            c = int(mm[3]) * 60 + int(mm[4])
            if c <= o:
                c += 1440
            periods.append((o, c))
        if sat in days:
            result = periods
    return result if result is not None else []


def _osm_ev(v: str | None) -> str:
    if v in {"yes", "only", "designated"}:
        return "confirmed"
    if v == "no":
        return "no"
    return "unknown"


def _snapshot_truth(raw: dict[str, Any]) -> Truth:
    info = cats()[raw["category"]]
    tags = raw.get("osm_tags", {})
    diet = {"vegetarian": _osm_ev(tags.get("diet:vegetarian")), "vegan": _osm_ev(tags.get("diet:vegan")),
            "jain": _osm_ev(tags.get("diet:jain"))}
    if diet["vegan"] == "confirmed" and diet["vegetarian"] == "unknown":
        diet["vegetarian"] = "confirmed"  # SOURCES.md: vegan options imply vegetarian options
    lo, hi = info["cost_inr"]
    if tags.get("fee") == "no" and info["group"] not in {"food", "drinks"}:
        lo = hi = 0
    popularity = raw.get("popularity", "medium")
    return Truth(
        id=raw["id"], name=raw["name"], category=raw["category"], lat=raw["lat"], lng=raw["lng"],
        cost_min=lo, cost_max=hi, hours=_osm_hours_saturday(tags.get("opening_hours")), diet=diet,
        wheelchair=_osm_ev(tags.get("wheelchair")), indoor=info["indoor"], seated=info["seated"],
        walking=info["walking"], popularity=popularity,
        busy=[] if popularity == "low" else [(int(w[0]), int(w[1])) for w in info["busy"]],
        extra={"last_checked": snapshot()["last_checked"]},
    )


def _event_truth(name: str, tz: str = "Asia/Kolkata") -> Truth | None:
    for scen, rec in events_fixture()["scenarios"].items():
        j = rec["jsonld"]
        if j["name"][:60] != name[:60]:
            continue
        zone = ZoneInfo(tz)
        start = datetime.fromisoformat(j["startDate"]).astimezone(zone)
        end = datetime.fromisoformat(j["endDate"]).astimezone(zone)
        s = start.hour * 60 + start.minute
        e = s + int((end - start).total_seconds() // 60)
        price = float(j.get("offers", {}).get("price", 0))
        geo = j.get("location", {}).get("geo", {})
        info = cats()["event"]
        return Truth(
            id="evfx:" + scen, name=j["name"], category="event", lat=geo.get("latitude"), lng=geo.get("longitude"),
            cost_min=int(price), cost_max=int(price), hours=[(s, e)], diet={}, wheelchair="unknown",
            indoor=info["indoor"], seated=info["seated"], walking=False, popularity="medium", busy=[],
            is_event=True, event_window=(s, e), event_date=start.date(),
            source_url=j.get("url") or rec.get("page_url"), extra={"scenario": scen, "page_url": rec.get("page_url")},
        )
    return None


def truth_for(cid: str, name: str) -> Truth | None:
    if cid.startswith("fx:"):
        raw = next((c for c in world()["candidates"] if c["id"] == cid), None)
        return _fixture_truth(raw) if raw else None
    if cid.startswith("snap:"):
        raw = next((p for p in snapshot()["places"] if p["id"] == cid), None)
        return _snapshot_truth(raw) if raw else None
    if cid.startswith("evfx:"):
        return _event_truth(name)
    return None


# ---------------------------------------------------------------------------------------------------
# Travel recomputation (§6: haversine x 1.4 / per-city speed + pickup; fares from cities.yaml)
# ---------------------------------------------------------------------------------------------------


def _city_cfg(city_name: str) -> dict[str, Any]:
    name = city_name.strip().lower()
    for key, entry in cities().items():
        if key.startswith("default"):
            continue
        if name == key or name in [a.lower() for a in entry.get("aliases") or []]:
            return entry
    return cities()["default_IN"]


def expected_leg(a: Truth, b: Truth, city_name: str) -> dict[str, Any]:
    cfg = _city_cfg(city_name)
    km = round(haversine_km(a.lat, a.lng, b.lat, b.lng) * DETOUR, 2)
    if km <= WALK_MAX_KM:
        minutes = math.ceil(km / cfg["speeds_kmh"]["walk"] * 60)
        return {"mode": "walk", "km": km, "minutes": minutes, "minutes_max": math.ceil(minutes * 1.3),
                "fare_min": 0, "fare_max": 0, "currency": "INR"}
    mode = cfg.get("motor_mode", "auto")
    fare = cfg["fares"][mode]
    minutes = math.ceil(cfg["pickup_min"].get(mode, 0) + km / cfg["speeds_kmh"][mode] * 60)
    amount = fare["base"] + max(0.0, km - fare.get("included_km", 0.0)) * fare["per_km"]
    return {"mode": mode, "km": km, "minutes": minutes, "minutes_max": math.ceil(minutes * 1.3),
            "fare_min": round(amount), "fare_max": round(amount * 1.25), "currency": fare["currency"]}


# ---------------------------------------------------------------------------------------------------
# Prose
# ---------------------------------------------------------------------------------------------------

_PROSE_BAD = [
    ("currency symbol", re.compile(r"[₹$£€¥]")),
    ("currency word with number", re.compile(r"\b(?:rs\.?|inr|rupees?|usd|gbp|eur|bucks)\s*\d|\d\s*(?:rs\b|inr\b|rupees?\b|/-)",
                                             re.I)),
    ("number with unit", re.compile(r"\d+(?:[.,]\d+)?\s*(?:min|mins|minutes?|hours?|hrs?|h|km|kms|am|pm|a\.m\.|p\.m\.|%|k)\b",
                                    re.I)),
    ("clock time", re.compile(r"\b\d{1,2}[:.]\d{2}\b|\bo'?clock\b", re.I)),
    ("word duration", re.compile(r"\b(?:an?|one|two|three|four|five|six|half an?|couple of|few|ninety|forty[- ]five|"
                                 r"thirty|twenty|fifteen|ten)\s+(?:hours?|minutes?|mins?)\b", re.I)),
]


def prose_problems(text: str | None) -> list[str]:
    if not text:
        return []
    return [kind for kind, rx in _PROSE_BAD if rx.search(text)]


# ---------------------------------------------------------------------------------------------------
# Plan recomputation
# ---------------------------------------------------------------------------------------------------

DIET_KEYS = {"vegetarian", "vegan", "jain"}


def is_food(category: str) -> bool:
    info = cats()[category]
    return info["group"] == "food" or bool(info["meal"])


def diet_ok(t: Truth, diet: str) -> bool:
    if diet == "vegetarian":
        return any(t.diet.get(k) == "confirmed" for k in DIET_KEYS)
    return t.diet.get(diet) == "confirmed"


def open_during(t: Truth, start: int, end: int) -> bool | None:
    if t.hours is None:
        return None
    for o, c in t.hours:
        if c <= o:
            c += 1440
        if o <= start and end <= c:
            return True
    return False


def likely_busy(t: Truth, start: int, end: int) -> bool:
    return t.popularity == "high" and any(start < we and ws < end for ws, we in t.busy)


@dataclass
class Expect:
    """What the case says, derived from the request text (independent of the parser)."""

    location_id: str
    budget: int | None
    currency: str = "INR"
    start_min: int | None = None  # None = accept the plan's start but check it is consistent
    available: int | None = None
    constraints: set[str] = field(default_factory=set)
    weather: str = "clear"


def constraint_keys_from_text(texts: list[str]) -> set[str]:
    keys: set[str] = set()
    for t in texts:
        low = t.lower()
        if "vegan" in low:
            keys.add("diet:vegan")
        elif "jain" in low:
            keys.add("diet:jain")
        elif re.search(r"(?<!non[- ])\bveg(?:etarian)?\b", low):
            keys.add("diet:vegetarian")
        if "wheelchair" in low:
            keys.add("access:wheelchair")
        if "crowd" in low:
            keys.add("crowd:avoid")
        if "sit-down meal" in low:
            keys.add("must_include:sit_down_meal")
    return keys


def sunset_truth(location_id: str, weather: str) -> int:
    """The sun sets whether or not the weather provider answered: use the clear-sky record when unavailable."""
    scen = weather_fixture()["scenarios"].get(weather) or weather_fixture()["scenarios"]["clear"]
    return int(scen[location_id]["sunset_min"])


def rain_truth(location_id: str, weather: str) -> bool:
    scen = weather_fixture()["scenarios"].get(weather)
    return bool(scen) and scen[location_id]["precip_prob_max"] >= 50


def plan_failures(result: PlanResult, exp: Expect) -> list[str]:
    """Every all_plan_assertions item that can be recomputed from the returned plan and the raw data."""
    out: list[str] = []
    plan = result.plan
    if plan is None:
        return ["no plan returned"]
    loc = location(exp.location_id)
    city_name = loc["name"]
    stops = plan.stops
    truths: list[Truth] = []

    # 1. IDs known (grounded) and in the requested city; embedded data matches the record
    for i, s in enumerate(stops):
        c = s.candidate
        t = truth_for(c.id, c.name)
        if t is None:
            out.append(f"stop {i} {c.id}: not a known fixture/snapshot/event record")
            continue
        truths.append(t)
        if c.city_id != exp.location_id:
            out.append(f"stop {i} {c.id}: city_id {c.city_id} != {exp.location_id}")
        if (c.cost.min, c.cost.max) != (t.cost_min, t.cost_max):
            out.append(f"stop {i} {c.id}: cost {c.cost.min}-{c.cost.max} != record {t.cost_min}-{t.cost_max}")
        if c.wheelchair.value != t.wheelchair:
            out.append(f"stop {i} {c.id}: wheelchair {c.wheelchair} != record {t.wheelchair}")
        for k, v in t.diet.items():
            if c.diet.get(k) is not None and c.diet[k].value != v:
                out.append(f"stop {i} {c.id}: diet {k}={c.diet[k]} != record {v}")
        d = haversine_km(c.lat, c.lng, loc["lat"], loc["lng"])
        if d > CITY_RADIUS_KM:
            out.append(f"stop {i} {c.id}: {d:.1f} km from the city centre")
        if c.cost.currency != exp.currency:
            out.append(f"stop {i} {c.id}: cost currency {c.cost.currency} != budget currency {exp.currency}")
    if len(truths) != len(stops):
        return out

    # 2. no duplicates (by ID and by name)
    ids = [s.candidate.id for s in stops]
    if len(set(ids)) != len(ids):
        out.append(f"duplicate stop IDs {ids}")
    names = [s.candidate.name.strip().lower() for s in stops]
    if len(set(names)) != len(names):
        out.append(f"duplicate stop names {names}")

    # 3. legs: count, endpoints, distance/mode/minutes/fare recomputed from §6
    if len(plan.legs) != len(stops) - 1:
        out.append(f"{len(plan.legs)} legs for {len(stops)} stops")
    for i, leg in enumerate(plan.legs):
        a, b = truths[i], truths[i + 1]
        if (leg.from_id, leg.to_id) != (stops[i].candidate.id, stops[i + 1].candidate.id):
            out.append(f"leg {i}: endpoints {leg.from_id}->{leg.to_id} do not match the stops")
        e = expected_leg(a, b, city_name)
        if leg.mode != e["mode"] or abs(leg.distance_km - e["km"]) > 0.02:
            out.append(f"leg {i}: {leg.mode} {leg.distance_km} km, expected {e['mode']} {e['km']} km")
        if abs(leg.minutes - e["minutes"]) > 1 or abs(leg.minutes_max - e["minutes_max"]) > 1:
            out.append(f"leg {i}: {leg.minutes}/{leg.minutes_max} min, expected {e['minutes']}/{e['minutes_max']}")
        if abs(leg.fare.max - e["fare_max"]) > 1 or abs(leg.fare.min - e["fare_min"]) > 1:
            out.append(f"leg {i}: fare {leg.fare.min}-{leg.fare.max}, expected {e['fare_min']}-{e['fare_max']}")
        if leg.fare.currency != exp.currency:
            out.append(f"leg {i}: fare currency {leg.fare.currency} != {exp.currency}")

    # 4. cost: worst case = stops max + legs max + ceil(10%) within budget
    upper = sum(t.cost_max for t in truths) + sum(leg.fare.max for leg in plan.legs)
    worst = upper + math.ceil(upper * 10 / 100)
    if plan.totals.worst_case != worst:
        out.append(f"totals.worst_case {plan.totals.worst_case} != recomputed {worst}")
    if exp.budget is not None and worst > exp.budget:
        out.append(f"worst case {worst} > budget {exp.budget}")

    # 5. schedule and elapsed time
    energy = result.preferences.energy if result.preferences else Energy.MEDIUM
    prof = PROFILES[energy]
    if exp.start_min is not None and plan.start_min != exp.start_min:
        out.append(f"plan starts at {plan.start_min}, expected {exp.start_min}")
    if stops[0].start_min < plan.start_min:
        out.append("first stop starts before the window")
    if not truths[0].is_event and stops[0].start_min != plan.start_min:
        out.append(f"first stop starts at {stops[0].start_min}, window starts {plan.start_min}")
    for i, s in enumerate(stops):
        if s.end_min - s.start_min != s.duration_min:
            out.append(f"stop {i}: end-start {s.end_min - s.start_min} != duration {s.duration_min}")
        if i > 0:
            arrival = stops[i - 1].end_min + plan.legs[i - 1].minutes_max + prof.buffer_minutes
            if truths[i].is_event:
                if arrival > truths[i].event_window[0] or s.start_min != truths[i].event_window[0]:  # type: ignore[index]
                    out.append(f"stop {i}: event arrival {arrival} / start {s.start_min} vs event start")
            elif s.start_min != arrival:
                out.append(f"stop {i}: starts {s.start_min}, recomputed arrival {arrival}")
    elapsed = stops[-1].end_min - plan.start_min
    available = exp.available if exp.available is not None else plan.totals.available_minutes
    if elapsed > available:
        out.append(f"elapsed {elapsed} > available {available}")

    # 6. opening hours (or an 'hours unverified' label)
    for i, (s, t) in enumerate(zip(stops, truths, strict=True)):
        if t.is_event:
            ws, we = t.event_window  # type: ignore[misc]
            if not (ws <= s.start_min and s.end_min <= we):
                out.append(f"stop {i}: event visit {s.start_min}-{s.end_min} outside {ws}-{we}")
            if t.event_date != TARGET:
                out.append(f"stop {i}: event date {t.event_date} != {TARGET}")
            continue
        status = open_during(t, s.start_min, s.end_min)
        if status is False:
            out.append(f"stop {i} {t.id}: {s.start_min}-{s.end_min} outside hours {t.hours}")
        elif status is None and not any("not verified" in w or "unverified" in w for w in s.warnings):
            out.append(f"stop {i} {t.id}: unknown hours without an 'hours unverified' label")

    # 7. hard constraints with positive evidence
    for i, (s, t) in enumerate(zip(stops, truths, strict=True)):
        for key in exp.constraints:
            kind, value = key.split(":")
            if kind == "diet" and is_food(t.category) and not diet_ok(t, value):
                out.append(f"stop {i} {t.id}: food stop without confirmed {value} evidence ({t.diet})")
            if kind == "access" and t.wheelchair != "confirmed":
                out.append(f"stop {i} {t.id}: wheelchair evidence {t.wheelchair}")
            if kind == "crowd" and likely_busy(t, s.start_min, s.end_min):
                out.append(f"stop {i} {t.id}: likely busy at {s.start_min}-{s.end_min}")
    if "must_include:sit_down_meal" in exp.constraints and not any(
            cats()[t.category]["meal"] and t.seated for t in truths):
        out.append("no seated sit-down meal stop")
    parsed = {c.key for c in (result.preferences.hard_constraints if result.preferences else [])}
    for key in exp.constraints:
        covered = key in parsed or (key == "diet:vegetarian" and parsed & {"diet:vegan", "diet:jain"})
        if not covered:
            out.append(f"constraint {key} from the request is missing from the enforced preferences")

    # 8. pacing
    if len(stops) > prof.max_stops:
        out.append(f"{len(stops)} stops > {prof.max_stops} for {energy}")
    walking = sum(leg.minutes for leg in plan.legs if leg.mode == "walk") + sum(
        s.duration_min for s, t in zip(stops, truths, strict=True) if t.walking)
    if walking > prof.max_walk_minutes:
        out.append(f"walking {walking} > {prof.max_walk_minutes}")
    long_legs = sum(1 for leg in plan.legs if leg.minutes > prof.long_leg_minutes)
    if long_legs > prof.max_long_legs:
        out.append(f"{long_legs} long legs > {prof.max_long_legs}")
    if sum(1 for t in truths if t.seated) < prof.min_seated_stops:
        out.append(f"fewer than {prof.min_seated_stops} seated stops")

    # 9. prose without numbers; why and source badge
    for i, s in enumerate(stops):
        for label, text in (("why", s.why), ("tradeoff", s.tradeoff)):
            for kind in prose_problems(text):
                out.append(f"stop {i} {label} has a {kind}: {text!r}")
        if not s.why.strip():
            out.append(f"stop {i}: empty why")
        src = s.candidate.source
        if src.provider not in {"google_places", "curated_snapshot", "fixture", "web_event", "fixture_event"}:
            out.append(f"stop {i}: unknown source {src.provider}")
        if src.provider == "curated_snapshot" and src.last_checked is None:
            out.append(f"stop {i}: snapshot stop without last_checked")
        if s.candidate.kind == "event" and not (src.url or "").startswith("https://"):
            out.append(f"stop {i}: event without an https source")
    for kind in prose_problems(plan.summary_note):
        out.append(f"summary_note has a {kind}: {plan.summary_note!r}")
    for o in plan.omitted:
        for kind in prose_problems(o.reason):
            out.append(f"omission reason has a {kind}: {o.reason!r}")

    # 10. daylight for outdoor walking stops (sunset from the fixture even when weather is 'unavailable')
    sunset = sunset_truth(exp.location_id, exp.weather)
    for i, (s, t) in enumerate(zip(stops, truths, strict=True)):
        if t.walking and not t.indoor and s.end_min > sunset:
            out.append(f"stop {i} {t.id}: outdoor walk ends {s.end_min} after sunset {sunset}")

    # 11. rain: every outdoor stop has an indoor, open, distinct, constraint-meeting Plan B
    if rain_truth(exp.location_id, exp.weather):
        for i, (s, t) in enumerate(zip(stops, truths, strict=True)):
            if t.indoor:
                continue
            pb = s.plan_b
            if pb is None:
                out.append(f"stop {i} {t.id}: outdoor in rain without a Plan B")
                continue
            pt = truth_for(pb.id, pb.name)
            if pt is None or not pt.indoor:
                out.append(f"stop {i}: Plan B {pb.id} is not a known indoor place")
                continue
            if pb.id in ids:
                out.append(f"stop {i}: Plan B {pb.id} is already a stop in this plan")
            if open_during(pt, s.start_min, s.end_min) is False:
                out.append(f"stop {i}: Plan B {pb.id} is closed during {s.start_min}-{s.end_min}")
            for key in exp.constraints:
                kind, value = key.split(":")
                if kind == "diet" and is_food(pt.category) and not diet_ok(pt, value):
                    out.append(f"stop {i}: Plan B {pb.id} lacks {value} evidence")
                if kind == "access" and pt.wheelchair != "confirmed":
                    out.append(f"stop {i}: Plan B {pb.id} lacks wheelchair evidence")
    return out


SECRET_MARKERS = ("SENTINEL", "sk-ant", "AIza", "tvly-")


def leaked(text: str) -> list[str]:
    return [m for m in SECRET_MARKERS if m in text]
