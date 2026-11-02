"""Authoritative plan validation (see IMPLEMENTATION_PLAN.md section 7). OWNER: domain agent.

Severity: everything is an error except HOURS_UNVERIFIED, WEATHER_RISK, CONSTRAINT_DROPPED and
ALCOHOL_SERVED, which are warnings. ``ValidationReport.ok`` is True when there are no errors.

Codes added beyond the original list:

* ``ALCOHOL_SERVED`` (warning): ``alcohol:avoid`` and a stop that serves alcohol without being an
  alcohol-focused venue (e.g. a restaurant with a bar). Alcohol-focused venues are ``ALCOHOL_CONFLICT``.

* ``CURRENCY_MISMATCH`` (error): a stop, Plan B or leg priced in a currency other than the budget currency
  (``prefs.currency``, else the location's). Amounts are never converted or mixed; the plan is not costed.
* ``LOCATION_APPROXIMATE`` (warning): a stop or Plan B the provider could not place (events without geo are
  put at the city centre and tagged ``approx_location``); the user must check the venue, legs are estimates.
* ``HOURS_UNVERIFIED`` is an error (not a warning) for concert halls, theatres and cinemas with unknown hours
  that are not events with times: they are only a plan when something is on.
* ``PROSE_NUMERIC_CLAIM`` also covers distances ("2.2 km", "500 m", "a 2km walk", "half a kilometre"); leg
  distances are shown from data. It ignores place names from the ledger ("Midnight Scoops" is a name, not a time).
* ``IDLE_WAIT`` (warning): a stop starts more than ``IDLE_WAIT_MIN`` minutes after the arrival time (waiting
  for an event); shown under the "time" check.
* ``PACING_RELAXED`` (warning): under ``setting:outdoors_only`` with no seated outdoor candidate in the ledger,
  the low-energy seated-stop rule is waived (otherwise "chill + outdoors only" could never be planned).

Plan B rules: known, indoors, distinct from every stop, open during the stop's slot (unknown hours warn), meets
the hard constraints, same currency, and the worst case with every Plan B taken (max(stop, Plan B) per stop)
fits the budget. Sunset falls back to a conservative computed value when the forecast has none.

``SCHEMA_INVALID`` is produced by ``draft_from_payload`` (callers parse raw tool arguments with it), for
candidates whose category is missing from categories.yaml, and for stops with an empty ``why``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.domain.categories import currency_symbol
from app.domain.constraints import merge_constraints
from app.domain.costs import worst_case
from app.domain.evidence import (
    PROGRAMME_CATEGORIES,
    candidate_allowed,
    category_info,
    check_constraint,
    event_window,
    has_approx_location,
    is_food_stop,
    is_outdoor,
    is_walking_stop,
    likely_busy,
    opening_status,
    satisfies_include,
)
from app.domain.schedule import build_schedule, plan_currency
from app.domain.schemas import (
    Candidate,
    CanonicalPlan,
    CheckItem,
    Constraint,
    DraftStop,
    PlanDraft,
    Preferences,
    ValidationReport,
    Violation,
    WorldContext,
)
from app.domain.sun import effective_sunset
from app.domain.travel import coords_valid, haversine_km
from app.providers.base import TravelEstimator

# code -> one-line meaning. Severity is decided per check; WEATHER_RISK and CONSTRAINT_DROPPED are warnings.
CODES: dict[str, str] = {
    "SCHEMA_INVALID": "Draft failed schema validation",
    "UNKNOWN_ID": "Candidate ID not in this run's ledger",
    "CITY_MISMATCH": "Candidate more than 40 km from the resolved city centre",
    "DUPLICATE_STOP": "Same candidate used twice",
    "DURATION_OUT_OF_RANGE": "Duration outside the candidate's allowed range",
    "BUDGET_EXCEEDED": "Worst-case cost above budget",
    "TIME_EXCEEDED": "Elapsed time above the available window",
    "HOURS_CONFLICT": "Visit overlaps a known closed period",
    "HOURS_UNVERIFIED": "Opening hours unknown (warning)",
    "DIET_UNVERIFIED": "Strict diet without positive evidence",
    "DIET_CONFLICT": "Contrary diet evidence",
    "ACCESSIBILITY_UNVERIFIED": "Wheelchair constraint without positive evidence",
    "ALCOHOL_CONFLICT": "Alcohol venue with alcohol:avoid",
    "SETTING_CONFLICT": "Indoor/outdoor setting constraint violated",
    "MUST_INCLUDE_MISSING": "A must_include requirement is not met",
    "MUST_AVOID_VIOLATED": "A must_avoid category is present",
    "CROWD_LIKELY": "Avoid-crowds constraint and a likely-busy stop at its scheduled time",
    "PACING_EXCEEDED": "Stops, walking or long legs above the energy profile",
    "DAYLIGHT_CONFLICT": "Outdoor walk scheduled after sunset",
    "WEATHER_RISK": "Outdoor stop with rain >= 50% and no Plan B (warning)",
    "EVENT_DATE_MISMATCH": "Event not on the target date",
    "SOURCE_MISSING": "Stop without a provider origin",
    "TRANSFER_UNKNOWN": "Leg could not be estimated (missing coordinates)",
    "PROSE_NUMERIC_CLAIM": "Currency amounts, durations, distances or clock times in model prose",
    "CONSTRAINT_DROPPED": "An original hard constraint was missing from parsed preferences (warning; still enforced)",
    "PLAN_B_INVALID": "Plan B candidate unknown, outdoor, or itself violating constraints",
    "ALCOHOL_SERVED": "Stop serves alcohol under alcohol:avoid but is not an alcohol venue (warning)",
    "CURRENCY_MISMATCH": "A stop, Plan B or leg is priced in a different currency from the budget",
    "PACING_RELAXED": "Low-energy seated-stop rule waived for 'outdoors only' (no seated outdoor options) (warning)",
    "LOCATION_APPROXIMATE": "Stop or Plan B has no real coordinates; venue location and legs are estimates (warning)",
    "IDLE_WAIT": "A stop starts more than 20 minutes after you arrive, e.g. waiting for an event (warning)",
}

WARNING_CODES = {"HOURS_UNVERIFIED", "WEATHER_RISK", "CONSTRAINT_DROPPED", "ALCOHOL_SERVED", "PACING_RELAXED",
                 "LOCATION_APPROXIMATE", "IDLE_WAIT"}
IDLE_WAIT_MIN = 20
CITY_RADIUS_KM = 40.0
EVENT_MIN_MINUTES = 30

_STATIC_CONSTRAINT_KINDS = {"diet", "access", "alcohol", "setting", "must_avoid"}


def _err(code: str, message: str, i: int | None = None, cid: str | None = None,
         severity: Literal["error", "warning"] | None = None) -> Violation:
    level = severity or ("warning" if code in WARNING_CODES else "error")
    return Violation(code=code, severity=level, message=message, stop_index=i, candidate_id=cid)


# ---------------------------------------------------------------------------------------------------
# Prose: no currency amounts, durations or clock times
# ---------------------------------------------------------------------------------------------------

_NUM = r"\d+(?:[.,]\d+)*"
_WORDNUM = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|"
    r"forty[- ]five|fifty|sixty|ninety|half an?|a couple of|couple of|an?|a few|few|several|a quarter of an?)"
)
_UNITS = r"(?:hours?|hrs?|minutes?|mins?)"
_PROSE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("currency amount", re.compile(
        rf"(?:₹|\$|£|€|¥|\brs\.?|\binr|\busd|\bgbp|\beur|\brupees?)\s*{_NUM}"
        rf"|{_NUM}\s*(?:₹|/-|rs\b\.?|inr\b|rupees?\b|bucks\b|dollars?\b|pounds?\b|euros?\b|usd\b|gbp\b|eur\b)"
        rf"|\b{_NUM}\s?k\b|\b{_NUM}\s*(?:lakhs?|lacs?|crores?)\b",
        re.IGNORECASE)),
    ("duration", re.compile(
        rf"\b{_NUM}[\s-]*{_UNITS}\b|\b{_NUM}h(?:{_NUM}m?)?\b"
        rf"|\b{_WORDNUM}[\s-]+{_UNITS}\b"
        r"|\b(?:half|quarter)[\s-]+hours?\b|\b(?:hours?|minutes?)[\s-]+long\b",
        re.IGNORECASE)),
    ("distance", re.compile(
        rf"\b{_NUM}\s*(?:km|kms|kilometres?|kilometers?|m|metres?|meters?|mi|miles?)\b"
        rf"|\b{_WORDNUM}[\s-]+(?:km|kms|kilometres?|kilometers?|metres?|meters?|miles?)\b",
        re.IGNORECASE)),
    ("clock time", re.compile(
        r"\b\d{1,2}(?:[:.][0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])"
        r"|\b(?:[01]?\d|2[0-3]):[0-5]\d\b"
        r"|\b\d{1,2}\s*o'?clock\b"
        r"|\b(?:noon|midday|midnight)\b",
        re.IGNORECASE)),
]


def name_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """Regex matching place names (and their leading two-or-more-word forms, e.g. "Midnight Scoops" for
    "Midnight Scoops Dessert Bar") so names are blanked before the numeric-claim check."""
    variants: set[str] = set()
    for name in names:
        words = name.split()
        if not words:
            continue
        if len(words) == 1:
            variants.add(words[0])
        for k in range(2, len(words) + 1):
            variants.add(" ".join(words[:k]))
    if not variants:
        return None
    alternation = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


def numeric_claims(text: str | None, names: re.Pattern[str] | None = None) -> list[str]:
    """Kinds of numeric claims found in ``text`` ("currency amount", "duration", "distance", "clock time"). Place names
    matched by ``names`` (see ``name_pattern``) are blanked first, so "Midnight Scoops" is not a clock time."""
    if not text:
        return []
    if names is not None:
        text = names.sub(" ", text)
    return [kind for kind, pattern in _PROSE_PATTERNS if pattern.search(text)]


def prose_violations(draft: PlanDraft, names: Iterable[str] = ()) -> list[Violation]:
    out: list[Violation] = []
    pattern = name_pattern(names)

    def check(text: str | None, where: str, i: int | None = None, cid: str | None = None) -> None:
        kinds = numeric_claims(text, pattern)
        if kinds:
            out.append(_err("PROSE_NUMERIC_CLAIM",
                            f"{where} contains a {' and a '.join(kinds)}; prices, durations and times come "
                            "from data, not prose", i, cid))

    for i, s in enumerate(draft.stops):
        check(s.why, "'why'", i, s.candidate_id)
        check(s.tradeoff, "'tradeoff'", i, s.candidate_id)
    check(draft.summary_note, "'summary_note'")
    for o in draft.omitted:
        check(o.reason, "An omission reason")
    return out


# ---------------------------------------------------------------------------------------------------
# Small checks shared with the deterministic planner
# ---------------------------------------------------------------------------------------------------


def url_is_https(url: str | None) -> bool:
    """True only for absolute https URLs with a host and no whitespace/control characters."""
    if not url or any(ch.isspace() or ord(ch) < 32 for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() == "https" and bool(parts.netloc)


def source_problems(c: Candidate) -> list[str]:
    """Reasons a candidate's source/link fields are unacceptable (empty when fine)."""
    problems = []
    if c.source.url is not None and not url_is_https(c.source.url):
        problems.append(f"{c.name} has a source link that is not a plain https URL")
    if c.maps_url is not None and not url_is_https(c.maps_url):
        problems.append(f"{c.name} has a map link that is not a plain https URL")
    if c.kind == "event" and not url_is_https(c.source.url):
        problems.append(f"The event {c.name} has no https source link")
    return problems


def distance_from_city_km(c: Candidate, ctx: WorldContext) -> float | None:
    if not coords_valid(c.lat, c.lng):
        return None
    return haversine_km(c.lat, c.lng, ctx.location.lat, ctx.location.lng)


def duration_bounds(c: Candidate) -> tuple[int, int]:
    """Allowed visit length: the candidate's duration_range, or 30..event length for events."""
    if c.kind == "event":
        window = event_window(c)
        if window is not None and c.event_end_min is not None:
            length = window[1] - window[0]
            return min(EVENT_MIN_MINUTES, length), length
        return min(EVENT_MIN_MINUTES, c.duration_range[2]), c.duration_range[2]
    return c.duration_range[0], c.duration_range[2]


def plan_b_problems(pb: Candidate | None, main: Candidate, constraints: list[Constraint],
                    ctx: WorldContext) -> list[str]:
    """Why a Plan B candidate is unusable (empty when valid). Time-independent checks only."""
    if pb is None:
        return ["The Plan B candidate ID was not returned by any tool in this run"]
    problems = []
    if pb.id == main.id:
        problems.append("Plan B is the same place as the stop")
    if pb.indoor is not True:
        problems.append(f"Plan B {pb.name} is not confirmed to be indoors")
    if category_info(pb) is None:
        problems.append(f"Plan B {pb.name} has an unknown category")
    dist = distance_from_city_km(pb, ctx)
    if dist is None or dist > CITY_RADIUS_KM:
        problems.append(f"Plan B {pb.name} is not in {ctx.location.name}")
    if pb.kind == "event" and pb.event_date != ctx.date:
        problems.append(f"Plan B {pb.name} is not on the target date")
    problems.extend(source_problems(pb))
    allowed, reasons = candidate_allowed(pb, constraints, ctx)
    if not allowed:
        problems.extend(reasons)
    return problems


def plan_b_timing_problems(pb: Candidate, start_min: int, end_min: int,
                           stop_ids: set[str]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a Plan B used during [start, end): it must be open then (unknown hours are a
    warning) and must not already be a stop in the plan."""
    errors: list[str] = []
    warnings: list[str] = []
    if pb.id in stop_ids:
        errors.append(f"Plan B {pb.name} is already a stop in this plan")
    status = opening_status(pb, start_min, end_min)
    if status == "closed":
        errors.append(f"Plan B {pb.name} is closed at that time")
    elif status == "unknown":
        warnings.append(f"Opening hours for Plan B {pb.name} are not verified")
    return errors, warnings


def worst_case_with_plan_b(stop_costs_max: list[int], plan_b_costs_max: list[int | None], legs_cost_max: int) -> int:
    """Worst case if every stop switches to its Plan B when that is pricier: max(stop, plan B) per stop."""
    upper = sum(max(s, b) if b is not None else s for s, b in zip(stop_costs_max, plan_b_costs_max, strict=True))
    return worst_case(upper, legs_cost_max)


def seated_rule_waived(constraints: list[Constraint], ledger: Mapping[str, Candidate], ctx: WorldContext) -> bool:
    """Under ``setting:outdoors_only`` a low-energy day's seated-stop rule is waived when no allowed candidate
    is both seated and outdoors (no outdoor category is seated by default)."""
    if ctx.pacing.min_seated_stops <= 0 or not any(c.key == "setting:outdoors_only" for c in constraints):
        return False
    return not any(
        cand.seated and cand.indoor is False and candidate_allowed(cand, constraints, ctx)[0]
        for cand in ledger.values()
    )


def _constraint_covered(orig: Constraint, parsed: list[Constraint]) -> bool:
    if orig in parsed:
        return True
    return orig.kind == "diet" and orig.value == "vegetarian" and any(
        p.kind == "diet" and p.value in {"vegan", "jain"} for p in parsed
    )


def dropped_constraints(parsed: list[Constraint], original: list[Constraint]) -> list[Constraint]:
    """Original hard constraints that the parsed preferences lost (a stricter diet counts as kept)."""
    return [c for c in original if not _constraint_covered(c, parsed)]


def draft_from_payload(payload: Any) -> tuple[PlanDraft | None, list[Violation]]:
    """Parse raw ``submit_plan`` arguments. Malformed input yields (None, [SCHEMA_INVALID]) and never renders."""
    try:
        if isinstance(payload, str | bytes):
            return PlanDraft.model_validate_json(payload), []
        return PlanDraft.model_validate(payload), []
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "draft" for e in exc.errors()})[:6]
        return None, [_err("SCHEMA_INVALID", "Draft failed schema validation at: " + ", ".join(fields))]


# ---------------------------------------------------------------------------------------------------
# validate_plan
# ---------------------------------------------------------------------------------------------------


def _static_stop_checks(i: int, stop: DraftStop, c: Candidate, ledger: Mapping[str, Candidate],
                        ctx: WorldContext, constraints: list[Constraint], currency: str) -> list[Violation]:
    out: list[Violation] = []
    if category_info(c) is None:
        out.append(_err("SCHEMA_INVALID", f"{c.name} has an unknown category '{c.category[:40]}'", i, c.id))
    dist = distance_from_city_km(c, ctx)
    if dist is not None and dist > CITY_RADIUS_KM:
        out.append(_err("CITY_MISMATCH",
                        f"{c.name} is {dist:.0f} km from the centre of {ctx.location.name}", i, c.id))
    for problem in source_problems(c):
        out.append(_err("SOURCE_MISSING", problem, i, c.id))
    if c.kind == "event" and c.event_date != ctx.date:
        when = c.event_date.isoformat() if c.event_date else "an unknown date"
        out.append(_err("EVENT_DATE_MISMATCH",
                        f"The event {c.name} is on {when}, not {ctx.date.isoformat()}", i, c.id))
    lo, hi = duration_bounds(c)
    if not lo <= stop.duration_min <= hi:
        out.append(_err("DURATION_OUT_OF_RANGE",
                        f"{stop.duration_min} min at {c.name} is outside the sensible {lo}-{hi} min", i, c.id))
    for con in constraints:
        if con.kind not in _STATIC_CONSTRAINT_KINDS:
            continue
        chk = check_constraint(c, con, ctx.start_min, ctx.end_min, ctx)
        if chk.code is not None:
            out.append(_err(chk.code, chk.message, i, c.id))
    if has_approx_location(c):
        out.append(_err("LOCATION_APPROXIMATE",
                        f"The exact location of {c.name} isn't listed; check the venue before you go. Travel to and "
                        "from it is estimated from the city centre", i, c.id))
    if stop.plan_b_candidate_id is not None:
        pb = ledger.get(stop.plan_b_candidate_id)
        if pb is not None and has_approx_location(pb):
            out.append(_err("LOCATION_APPROXIMATE",
                            f"The exact location of Plan B {pb.name} isn't listed; check the venue before you go",
                            i, c.id))
        for problem in plan_b_problems(pb, c, constraints, ctx):
            out.append(_err("PLAN_B_INVALID", problem, i, c.id))
        if pb is not None and pb.cost.currency != currency:
            out.append(_err("CURRENCY_MISMATCH",
                            f"Plan B {pb.name} is priced in {pb.cost.currency}, not the budget's {currency}", i, c.id))
    return out


def _scheduled_checks(plan: CanonicalPlan, draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext,
                      prefs: Preferences, constraints: list[Constraint], late_events: set[int]) -> list[Violation]:
    out: list[Violation] = []
    pacing = ctx.pacing
    totals = plan.totals
    sym = currency_symbol(totals.currency)
    sunset = effective_sunset(ctx)
    stop_ids = {s.candidate.id for s in plan.stops}
    plan_b_costs: list[int | None] = []

    for i, (s, ds) in enumerate(zip(plan.stops, draft.stops, strict=True)):
        c = s.candidate
        arrival = ctx.start_min if i == 0 else (
            plan.stops[i - 1].end_min + plan.legs[i - 1].minutes_max + pacing.buffer_minutes)
        if s.start_min - arrival > IDLE_WAIT_MIN:
            out.append(_err("IDLE_WAIT",
                            f"You'd wait about {s.start_min - arrival} min before {c.name} starts", i, c.id))
        status = opening_status(c, s.start_min, s.end_min)
        if status == "closed" and i not in late_events:
            what = "the event's time" if c.kind == "event" else "its known opening hours"
            out.append(_err("HOURS_CONFLICT", f"The visit to {c.name} falls outside {what}", i, c.id))
        elif status == "unknown" and c.kind == "place" and c.category in PROGRAMME_CATEGORIES:
            out.append(_err("HOURS_UNVERIFIED",
                            f"{c.name} is only worth visiting when something is on, and no show times are listed; "
                            "use an event with times instead", i, c.id, severity="error"))
        elif status == "unknown":
            out.append(_err("HOURS_UNVERIFIED", f"Opening hours for {c.name} are not verified", i, c.id))
        for con in constraints:
            if con.kind == "crowd" and con.value == "avoid":
                chk = check_constraint(c, con, s.start_min, s.end_min, ctx)
                if chk.code is not None:
                    out.append(_err(chk.code, chk.message, i, c.id))
        outdoor = is_outdoor(c)
        if sunset is not None and outdoor and is_walking_stop(c) and s.end_min > sunset:
            note = "" if ctx.weather.sunset_min is not None else " (computed sunset; weather unavailable)"
            out.append(_err("DAYLIGHT_CONFLICT", f"The outdoor walk at {c.name} would end after sunset{note}", i, c.id))

        plan_b_ok = False
        pb = s.plan_b if ds.plan_b_candidate_id is not None else None
        if pb is not None:
            errors, warnings = plan_b_timing_problems(pb, s.start_min, s.end_min, stop_ids)
            for msg in errors:
                out.append(_err("PLAN_B_INVALID", msg, i, c.id))
            for msg in warnings:
                out.append(_err("HOURS_UNVERIFIED", msg, i, c.id))
            plan_b_ok = (not errors and not plan_b_problems(pb, c, constraints, ctx)
                         and pb.cost.currency == totals.currency)
        plan_b_costs.append(pb.cost.max if pb is not None and pb.cost.currency == totals.currency else None)
        if ctx.weather.is_rainy and outdoor and not plan_b_ok:
            out.append(_err("WEATHER_RISK",
                            f"Rain is likely and {c.name} is outdoors with no valid indoor Plan B", i, c.id))

    if prefs.budget is not None and totals.worst_case > prefs.budget:
        out.append(_err("BUDGET_EXCEEDED",
                        f"Worst case {sym}{totals.worst_case} (with contingency) is above the budget of "
                        f"{sym}{prefs.budget}"))
    elif prefs.budget is not None and any(b is not None for b in plan_b_costs):
        switched = worst_case_with_plan_b([s.candidate.cost.max for s in plan.stops], plan_b_costs,
                                          totals.legs_cost_max)
        if switched > prefs.budget:
            for i, (s, b) in enumerate(zip(plan.stops, plan_b_costs, strict=True)):
                if b is not None and b > s.candidate.cost.max and s.plan_b is not None:
                    out.append(_err("PLAN_B_INVALID",
                                    f"Switching to Plan B {s.plan_b.name} could take the worst case to "
                                    f"{sym}{switched}, above the {sym}{prefs.budget} budget", i, s.candidate.id))
    if totals.elapsed_min > prefs.available_minutes:
        out.append(_err("TIME_EXCEEDED",
                        f"The plan takes {totals.elapsed_min} min but only {prefs.available_minutes} min are free"))
    elif plan.end_min > ctx.end_min:
        out.append(_err("TIME_EXCEEDED", "The plan ends after the end of your window"))

    energy = pacing.energy.value
    n = len(plan.stops)
    if n > pacing.max_stops:
        out.append(_err("PACING_EXCEEDED", f"{n} stops is more than the {pacing.max_stops} a {energy}-energy day allows"))
    if totals.walking_min > pacing.max_walk_minutes:
        out.append(_err("PACING_EXCEEDED",
                        f"{totals.walking_min} min of walking is above the {pacing.max_walk_minutes} min limit "
                        f"for a {energy}-energy day"))
    long_legs = sum(1 for leg in plan.legs if leg.minutes > pacing.long_leg_minutes)
    if long_legs > pacing.max_long_legs:
        out.append(_err("PACING_EXCEEDED",
                        f"{long_legs} legs are longer than {pacing.long_leg_minutes} min; at most "
                        f"{pacing.max_long_legs} allowed for a {energy}-energy day"))
    seated = sum(1 for s in plan.stops if s.candidate.seated)
    if seated < pacing.min_seated_stops:
        if seated_rule_waived(constraints, ledger, ctx):
            out.append(_err("PACING_RELAXED",
                            f"No seated outdoor options were found, so the seated-stop rule for a {energy}-energy "
                            "day was relaxed to keep 'outdoors only'; plan your own rest breaks"))
        else:
            out.append(_err("PACING_EXCEEDED",
                            f"A {energy}-energy day needs at least {pacing.min_seated_stops} seated stop(s)"))

    for con in constraints:
        if con.kind == "must_include" and not any(satisfies_include(s.candidate, con.value) for s in plan.stops):
            out.append(_err("MUST_INCLUDE_MISSING", f"No stop provides '{con.value.replace('_', ' ')}'"))
    return out


def validate_plan(draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext, prefs: Preferences,
                  original_constraints: list[Constraint], travel: TravelEstimator) -> ValidationReport:
    """Recompute everything from ledger records; enforce merge(prefs.hard_constraints, original_constraints).
    Never trusts model-provided numbers. Pure function."""
    constraints = merge_constraints(prefs.hard_constraints, original_constraints)
    currency = plan_currency(ctx, prefs)
    violations: list[Violation] = []

    for dropped in dropped_constraints(prefs.hard_constraints, original_constraints):
        violations.append(_err("CONSTRAINT_DROPPED",
                               f"'{dropped.source_text or dropped.key}' was missing from the parsed preferences; it is still "
                               "enforced"))
    violations.extend(prose_violations(draft, [c.name for c in ledger.values()]))

    seen: set[str] = set()
    for i, s in enumerate(draft.stops):
        if not s.why.strip():
            violations.append(_err("SCHEMA_INVALID", "Every stop needs a non-empty 'why'", i, s.candidate_id[:120]))
        if s.candidate_id in seen:
            violations.append(_err("DUPLICATE_STOP", "The same place appears twice", i, s.candidate_id[:120]))
        seen.add(s.candidate_id)
        c = ledger.get(s.candidate_id)
        if c is not None:
            violations.extend(_static_stop_checks(i, s, c, ledger, ctx, constraints, currency))

    plan, schedule_violations = build_schedule(draft, ledger, ctx, prefs, travel)
    violations.extend(schedule_violations)
    if plan is not None:
        late_events = {v.stop_index for v in schedule_violations if v.code == "HOURS_CONFLICT" and v.stop_index is not None}
        violations.extend(_scheduled_checks(plan, draft, ledger, ctx, prefs, constraints, late_events))
        for v in violations:
            if v.severity == "warning" and v.stop_index is not None and v.stop_index < len(plan.stops):
                stop_warnings = plan.stops[v.stop_index].warnings
                if v.message not in stop_warnings:
                    stop_warnings.append(v.message)
    ok = not any(v.severity == "error" for v in violations)
    return ValidationReport(ok=ok, violations=violations, plan=plan)


# ---------------------------------------------------------------------------------------------------
# Checks panel
# ---------------------------------------------------------------------------------------------------


def _codes(report: ValidationReport) -> dict[str, list[Violation]]:
    out: dict[str, list[Violation]] = {}
    for v in report.violations:
        out.setdefault(v.code, []).append(v)
    return out


def summarize_checks(report: ValidationReport, ctx: WorldContext, prefs: Preferences,
                     constraints: list[Constraint]) -> list[CheckItem]:
    """Checks panel: budget, time, opening_hours, diet, accessibility, crowds, weather, sources."""
    codes = _codes(report)
    plan = report.plan
    items: list[CheckItem] = []
    keys = {c.key for c in constraints}

    # budget
    if "CURRENCY_MISMATCH" in codes:
        items.append(CheckItem(name="budget", status="fail", detail=codes["CURRENCY_MISMATCH"][0].message))
    elif plan is None:
        items.append(CheckItem(name="budget", status="unverified", detail="The plan could not be costed"))
    else:
        t = plan.totals
        sym = currency_symbol(t.currency)
        if "BUDGET_EXCEEDED" in codes:
            items.append(CheckItem(name="budget", status="fail",
                                   detail=f"Worst case {sym}{t.worst_case} is above your {sym}{t.budget} budget"))
        elif t.budget is None:
            items.append(CheckItem(name="budget", status="unverified",
                                   detail=f"No budget given; worst case {sym}{t.worst_case} incl. contingency"))
        else:
            items.append(CheckItem(name="budget", status="pass",
                                   detail=f"Worst case {sym}{t.worst_case} incl. {sym}{t.contingency} contingency; "
                                          f"{sym}{t.budget_left} to spare"))
    # time
    if plan is None:
        items.append(CheckItem(name="time", status="unverified", detail="The plan could not be scheduled"))
    elif "TIME_EXCEEDED" in codes:
        items.append(CheckItem(name="time", status="fail", detail=codes["TIME_EXCEEDED"][0].message))
    else:
        t = plan.totals
        detail = f"Uses {t.elapsed_min} of {t.available_minutes} min including travel and buffers"
        if "IDLE_WAIT" in codes:
            detail += "; " + codes["IDLE_WAIT"][0].message[0].lower() + codes["IDLE_WAIT"][0].message[1:]
        if "PACING_RELAXED" in codes:
            detail += "; no seated stop because nothing seated is outdoors"
        warn = "PACING_RELAXED" in codes or "IDLE_WAIT" in codes
        items.append(CheckItem(name="time", status="warn" if warn else "pass", detail=detail))
    # opening hours
    if "HOURS_CONFLICT" in codes:
        items.append(CheckItem(name="opening_hours", status="fail", detail=codes["HOURS_CONFLICT"][0].message))
    elif plan is None:
        items.append(CheckItem(name="opening_hours", status="unverified", detail="The plan could not be scheduled"))
    elif "HOURS_UNVERIFIED" in codes:
        n = len(codes["HOURS_UNVERIFIED"])
        items.append(CheckItem(name="opening_hours", status="unverified",
                               detail=f"Hours not verified for {n} stop{'s' if n != 1 else ''}; check before you go"))
    else:
        items.append(CheckItem(name="opening_hours", status="pass", detail="Every stop is open at its scheduled time"))
    # diet
    diets = [c for c in constraints if c.kind == "diet"]
    if "DIET_CONFLICT" in codes:
        items.append(CheckItem(name="diet", status="fail", detail=codes["DIET_CONFLICT"][0].message))
    elif "DIET_UNVERIFIED" in codes:
        items.append(CheckItem(name="diet", status="unverified", detail=codes["DIET_UNVERIFIED"][0].message))
    elif not diets:
        items.append(CheckItem(name="diet", status="pass", detail="No diet requirement"))
    elif plan is None:
        items.append(CheckItem(name="diet", status="unverified", detail="The plan could not be built"))
    elif not any(is_food_stop(s.candidate) for s in plan.stops):
        items.append(CheckItem(name="diet", status="pass", detail="No food stops in this plan"))
    else:
        label = " and ".join(d.value for d in diets)
        items.append(CheckItem(name="diet", status="pass", detail=f"{label.capitalize()} confirmed at every food stop"))
    # accessibility
    if "access:wheelchair" not in keys:
        items.append(CheckItem(name="accessibility", status="pass", detail="No accessibility requirement"))
    elif "ACCESSIBILITY_UNVERIFIED" in codes:
        items.append(CheckItem(name="accessibility", status="unverified",
                               detail=codes["ACCESSIBILITY_UNVERIFIED"][0].message))
    elif plan is None:
        items.append(CheckItem(name="accessibility", status="unverified", detail="The plan could not be built"))
    else:
        items.append(CheckItem(name="accessibility", status="pass", detail="Wheelchair access confirmed at every stop"))
    # crowds
    if "CROWD_LIKELY" in codes:
        items.append(CheckItem(name="crowds", status="fail", detail=codes["CROWD_LIKELY"][0].message))
    elif plan is None:
        items.append(CheckItem(name="crowds", status="unverified", detail="The plan could not be scheduled"))
    else:
        busy = [s for s in plan.stops if likely_busy(s.candidate, s.start_min, s.end_min)]
        if busy:
            items.append(CheckItem(name="crowds", status="warn",
                                   detail=f"{len(busy)} stop(s) likely busy at their time (heuristic)"))
        else:
            items.append(CheckItem(name="crowds", status="pass",
                                   detail="No stop is likely busy at its scheduled time (heuristic)"))
    # weather
    w = ctx.weather
    if "DAYLIGHT_CONFLICT" in codes:
        items.append(CheckItem(name="weather", status="fail", detail=codes["DAYLIGHT_CONFLICT"][0].message))
    elif w.status != "ok":
        items.append(CheckItem(name="weather", status="unverified", detail="Weather not checked"))
    elif "WEATHER_RISK" in codes:
        items.append(CheckItem(name="weather", status="warn", detail=codes["WEATHER_RISK"][0].message))
    elif w.is_rainy:
        outdoor = plan is not None and any(is_outdoor(s.candidate) for s in plan.stops)
        items.append(CheckItem(name="weather", status="pass",
                               detail=f"Rain likely ({w.precip_prob_max}%); "
                                      + ("outdoor stops have an indoor Plan B" if outdoor else "every stop is indoors")))
    else:
        detail = w.summary or "Forecast checked"
        if w.precip_prob_max is not None and "rain" not in detail.lower() and "%" not in detail:
            detail += f"; {w.precip_prob_max}% chance of rain"
        items.append(CheckItem(name="weather", status="pass", detail=detail))
    # sources
    source_codes = [c for c in ("UNKNOWN_ID", "SOURCE_MISSING", "EVENT_DATE_MISMATCH", "CITY_MISMATCH") if c in codes]
    if source_codes:
        items.append(CheckItem(name="sources", status="fail", detail=codes[source_codes[0]][0].message))
    elif plan is None:
        items.append(CheckItem(name="sources", status="unverified", detail="The plan could not be built"))
    elif "LOCATION_APPROXIMATE" in codes:
        items.append(CheckItem(name="sources", status="warn", detail=codes["LOCATION_APPROXIMATE"][0].message))
    else:
        providers: dict[str, int] = {}
        for s in plan.stops:
            providers[s.candidate.source.provider] = providers.get(s.candidate.source.provider, 0) + 1
        label = ", ".join(f"{n} {p.replace('_', ' ')}" for p, n in sorted(providers.items()))
        items.append(CheckItem(name="sources", status="pass", detail=f"Every stop has a source ({label})"))
    return items


__all__ = [
    "CODES", "WARNING_CODES", "draft_from_payload", "dropped_constraints", "duration_bounds", "numeric_claims",
    "plan_b_problems", "prose_violations", "source_problems", "summarize_checks", "url_is_https", "validate_plan",
]
