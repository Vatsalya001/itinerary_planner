"""Authoritative plan validation (see IMPLEMENTATION_PLAN.md section 7). OWNER: domain agent.

Severity: everything is an error except HOURS_UNVERIFIED, WEATHER_RISK, CONSTRAINT_DROPPED and
ALCOHOL_SERVED, which are warnings. ``ValidationReport.ok`` is True when there are no errors.

Codes added beyond the original list:

* ``ALCOHOL_SERVED`` (warning): ``alcohol:avoid`` and a stop that serves alcohol without being an
  alcohol-focused venue (e.g. a restaurant with a bar). Alcohol-focused venues are ``ALCOHOL_CONFLICT``.

``SCHEMA_INVALID`` is produced by ``draft_from_payload`` (callers parse raw tool arguments with it), for
candidates whose category is missing from categories.yaml, and for stops with an empty ``why``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.domain.categories import currency_symbol
from app.domain.constraints import merge_constraints
from app.domain.evidence import (
    candidate_allowed,
    category_info,
    check_constraint,
    event_window,
    is_food_stop,
    is_outdoor,
    is_walking_stop,
    likely_busy,
    opening_status,
    satisfies_include,
)
from app.domain.schedule import build_schedule
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
    "PROSE_NUMERIC_CLAIM": "Currency amounts, durations or clock times in model prose",
    "CONSTRAINT_DROPPED": "An original hard constraint was missing from parsed preferences (warning; still enforced)",
    "PLAN_B_INVALID": "Plan B candidate unknown, outdoor, or itself violating constraints",
    "ALCOHOL_SERVED": "Stop serves alcohol under alcohol:avoid but is not an alcohol venue (warning)",
}

WARNING_CODES = {"HOURS_UNVERIFIED", "WEATHER_RISK", "CONSTRAINT_DROPPED", "ALCOHOL_SERVED"}
CITY_RADIUS_KM = 40.0
EVENT_MIN_MINUTES = 30

_STATIC_CONSTRAINT_KINDS = {"diet", "access", "alcohol", "setting", "must_avoid"}


def _err(code: str, message: str, i: int | None = None, cid: str | None = None) -> Violation:
    return Violation(code=code, severity="warning" if code in WARNING_CODES else "error", message=message,
                     stop_index=i, candidate_id=cid)


# ---------------------------------------------------------------------------------------------------
# Prose: no currency amounts, durations or clock times
# ---------------------------------------------------------------------------------------------------

_NUM = r"\d+(?:[.,]\d+)*"
_WORDNUM = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|"
    r"forty[- ]five|fifty|sixty|ninety|half an?|a couple of|couple of)"
)
_PROSE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("currency amount", re.compile(
        rf"(?:₹|\$|£|€|¥|\brs\.?|\binr|\busd|\bgbp|\beur|\brupees?)\s*{_NUM}"
        rf"|{_NUM}\s*(?:₹|/-|rs\b\.?|inr\b|rupees?\b|bucks\b|dollars?\b|pounds?\b|euros?\b|usd\b|gbp\b|eur\b)",
        re.IGNORECASE)),
    ("duration", re.compile(
        rf"\b{_NUM}[\s-]*(?:hours?|hrs?|minutes?|mins?)\b|\b{_NUM}h(?:{_NUM}m?)?\b"
        rf"|\b{_WORDNUM}[\s-]+(?:hours?|hrs?|minutes?|mins?)\b",
        re.IGNORECASE)),
    ("clock time", re.compile(
        r"\b\d{1,2}(?:[:.][0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])"
        r"|\b(?:[01]?\d|2[0-3]):[0-5]\d\b"
        r"|\b\d{1,2}\s*o'?clock\b",
        re.IGNORECASE)),
]


def numeric_claims(text: str | None) -> list[str]:
    """Kinds of numeric claims found in ``text`` ("currency amount", "duration", "clock time")."""
    if not text:
        return []
    return [kind for kind, pattern in _PROSE_PATTERNS if pattern.search(text)]


def prose_violations(draft: PlanDraft) -> list[Violation]:
    out: list[Violation] = []

    def check(text: str | None, where: str, i: int | None = None, cid: str | None = None) -> None:
        kinds = numeric_claims(text)
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
                        ctx: WorldContext, constraints: list[Constraint]) -> list[Violation]:
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
    if stop.plan_b_candidate_id is not None:
        for problem in plan_b_problems(ledger.get(stop.plan_b_candidate_id), c, constraints, ctx):
            out.append(_err("PLAN_B_INVALID", problem, i, c.id))
    return out


def _scheduled_checks(plan: CanonicalPlan, draft: PlanDraft, ctx: WorldContext, prefs: Preferences,
                      constraints: list[Constraint], late_events: set[int]) -> list[Violation]:
    out: list[Violation] = []
    pacing = ctx.pacing
    totals = plan.totals
    sym = currency_symbol(totals.currency)
    sunset = ctx.weather.sunset_min

    for i, (s, ds) in enumerate(zip(plan.stops, draft.stops, strict=True)):
        c = s.candidate
        status = opening_status(c, s.start_min, s.end_min)
        if status == "closed" and i not in late_events:
            what = "the event's time" if c.kind == "event" else "its known opening hours"
            out.append(_err("HOURS_CONFLICT", f"The visit to {c.name} falls outside {what}", i, c.id))
        elif status == "unknown":
            out.append(_err("HOURS_UNVERIFIED", f"Opening hours for {c.name} are not verified", i, c.id))
        for con in constraints:
            if con.kind == "crowd" and con.value == "avoid":
                chk = check_constraint(c, con, s.start_min, s.end_min, ctx)
                if chk.code is not None:
                    out.append(_err(chk.code, chk.message, i, c.id))
        outdoor = is_outdoor(c)
        if sunset is not None and outdoor and is_walking_stop(c) and s.end_min > sunset:
            out.append(_err("DAYLIGHT_CONFLICT", f"The outdoor walk at {c.name} would end after sunset", i, c.id))
        if ctx.weather.is_rainy and outdoor:
            pb_id = ds.plan_b_candidate_id
            if pb_id is None or s.plan_b is None or plan_b_problems(s.plan_b, c, constraints, ctx):
                out.append(_err("WEATHER_RISK",
                                f"Rain is likely and {c.name} is outdoors with no valid indoor Plan B", i, c.id))

    if prefs.budget is not None and totals.worst_case > prefs.budget:
        out.append(_err("BUDGET_EXCEEDED",
                        f"Worst case {sym}{totals.worst_case} (with contingency) is above the budget of "
                        f"{sym}{prefs.budget}"))
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
    violations: list[Violation] = []

    for dropped in dropped_constraints(prefs.hard_constraints, original_constraints):
        violations.append(_err("CONSTRAINT_DROPPED",
                               f"'{dropped.source_text or dropped.key}' was missing from the parsed preferences; it is still "
                               "enforced"))
    violations.extend(prose_violations(draft))

    seen: set[str] = set()
    for i, s in enumerate(draft.stops):
        if not s.why.strip():
            violations.append(_err("SCHEMA_INVALID", "Every stop needs a non-empty 'why'", i, s.candidate_id[:120]))
        if s.candidate_id in seen:
            violations.append(_err("DUPLICATE_STOP", "The same place appears twice", i, s.candidate_id[:120]))
        seen.add(s.candidate_id)
        c = ledger.get(s.candidate_id)
        if c is not None:
            violations.extend(_static_stop_checks(i, s, c, ledger, ctx, constraints))

    plan, schedule_violations = build_schedule(draft, ledger, ctx, prefs, travel)
    violations.extend(schedule_violations)
    if plan is not None:
        late_events = {v.stop_index for v in schedule_violations if v.code == "HOURS_CONFLICT" and v.stop_index is not None}
        violations.extend(_scheduled_checks(plan, draft, ctx, prefs, constraints, late_events))
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
    if plan is None:
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
        items.append(CheckItem(name="time", status="pass",
                               detail=f"Uses {t.elapsed_min} of {t.available_minutes} min including travel and buffers"))
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
        items.append(CheckItem(name="weather", status="pass",
                               detail=f"Rain likely ({w.precip_prob_max}%); outdoor stops have an indoor Plan B"))
    else:
        rain = f"{w.precip_prob_max}% chance of rain" if w.precip_prob_max is not None else "forecast checked"
        items.append(CheckItem(name="weather", status="pass", detail=f"{w.summary or 'Forecast checked'}; {rain}"))
    # sources
    source_codes = [c for c in ("UNKNOWN_ID", "SOURCE_MISSING", "EVENT_DATE_MISMATCH", "CITY_MISMATCH") if c in codes]
    if source_codes:
        items.append(CheckItem(name="sources", status="fail", detail=codes[source_codes[0]][0].message))
    elif plan is None:
        items.append(CheckItem(name="sources", status="unverified", detail="The plan could not be built"))
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
