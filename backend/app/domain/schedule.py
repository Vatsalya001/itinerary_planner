"""Hydrate a PlanDraft into a CanonicalPlan: clock times, legs, totals. OWNER: domain agent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlencode

from app.domain.categories import currency_for_country
from app.domain.costs import compute_totals, foreign_currencies
from app.domain.evidence import is_walking_stop
from app.domain.schemas import (
    Candidate,
    CanonicalPlan,
    Leg,
    Omission,
    PlanDraft,
    Preferences,
    ScheduledStop,
    Violation,
    WorldContext,
)
from app.domain.travel import coords_valid
from app.providers.base import TravelEstimator

MAPS_DIR_URL = "https://www.google.com/maps/dir/"


def plan_currency(ctx: WorldContext, prefs: Preferences, stops: Sequence[Candidate] = ()) -> str:
    """The budget currency: the user's currency, else the location's (location, country, else USD - the same
    rule providers and travel use to price things). ``stops`` is accepted for compatibility and ignored, so a
    stop's own currency can never redefine the budget currency."""
    if prefs.currency:
        return prefs.currency
    return ctx.location.currency or currency_for_country(ctx.location.country_code) or "USD"


def stop_start(c: Candidate, arrival_min: int) -> int:
    """When a stop starts given the arrival time: events start at their start time (waiting allowed)."""
    if c.kind == "event" and c.event_start_min is not None and arrival_min <= c.event_start_min:
        return c.event_start_min
    return arrival_min


def arrives_late_to_event(c: Candidate, arrival_min: int) -> bool:
    return c.kind == "event" and c.event_start_min is not None and arrival_min > c.event_start_min


def _point(c: Candidate) -> str:
    return f"{c.lat:.6f},{c.lng:.6f}"


def directions_url(stops: Sequence[Candidate]) -> str | None:
    """Google Maps directions URL (Maps URLs API, no key): origin, destination, waypoints, driving."""
    if not stops:
        return None
    params: dict[str, str] = {"api": "1"}
    if len(stops) == 1:
        params["destination"] = _point(stops[0])
    else:
        params["origin"] = _point(stops[0])
        params["destination"] = _point(stops[-1])
        if len(stops) > 2:
            params["waypoints"] = "|".join(_point(c) for c in stops[1:-1])
    params["travelmode"] = "driving"
    return MAPS_DIR_URL + "?" + urlencode(params)


def named_omissions(omitted: Sequence[Omission], ledger: Mapping[str, Candidate]) -> list[Omission]:
    """Omissions with ``name`` filled server-side from the ledger when ``ref`` is a candidate ID (never trusts a
    model-supplied name for a known ID)."""
    out = []
    for o in omitted:
        cand = ledger.get(o.ref)
        out.append(o.model_copy(update={"name": cand.name[:120]}) if cand is not None else o)
    return out


def build_schedule(draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext, prefs: Preferences,
                   travel: TravelEstimator) -> tuple[CanonicalPlan | None, list[Violation]]:
    """start = ctx.start_min; each stop arrives at prev end + leg.minutes_max + pacing buffer.
    Events start at event_start_min (waiting is allowed and counts toward elapsed time).
    Returns (None, [UNKNOWN_ID/SCHEMA violations]) when hydration fails. Totals use ceil(10%) contingency.
    Also builds a Google Maps directions URL for the route.

    Violations returned here: UNKNOWN_ID (hydration), TRANSFER_UNKNOWN (missing coordinates; no plan),
    CURRENCY_MISMATCH (a stop or leg not priced in the budget currency; the plan is returned for feedback but
    its totals leave the foreign amounts out - they are never added as if they were the budget currency),
    and HOURS_CONFLICT when the arrival at an event is after the event's start (plan still returned).
    """
    violations: list[Violation] = []
    candidates: list[Candidate] = []
    for i, stop in enumerate(draft.stops):
        cand = ledger.get(stop.candidate_id)
        if cand is None:
            violations.append(Violation(
                code="UNKNOWN_ID", severity="error", stop_index=i, candidate_id=stop.candidate_id[:120],
                message="This candidate ID was not returned by any tool in this run",
            ))
            continue
        candidates.append(cand)
    if violations:
        return None, violations

    for i, cand in enumerate(candidates):
        if not coords_valid(cand.lat, cand.lng):
            violations.append(Violation(
                code="TRANSFER_UNKNOWN", severity="error", stop_index=i, candidate_id=cand.id,
                message=f"{cand.name} has no usable coordinates, so travel to or from it can't be estimated",
            ))
    if violations:
        return None, violations

    buffer = ctx.pacing.buffer_minutes
    legs: list[Leg] = []
    stops: list[ScheduledStop] = []
    walking = 0
    prev_end = ctx.start_min
    for i, (stop, cand) in enumerate(zip(draft.stops, candidates, strict=True)):
        if i == 0:
            arrival = ctx.start_min
        else:
            try:
                leg = travel.leg(candidates[i - 1], cand, ctx)
            except ValueError:  # TransferUnknownError or another estimator's bad-input error
                violations.append(Violation(
                    code="TRANSFER_UNKNOWN", severity="error", stop_index=i, candidate_id=cand.id,
                    message=f"Travel to {cand.name} could not be estimated",
                ))
                return None, violations
            legs.append(leg)
            if leg.mode == "walk":
                walking += leg.minutes
            arrival = prev_end + leg.minutes_max + buffer
        if arrives_late_to_event(cand, arrival):
            violations.append(Violation(
                code="HOURS_CONFLICT", severity="error", stop_index=i, candidate_id=cand.id,
                message=f"You would arrive at {cand.name} after the event has started",
            ))
        start = stop_start(cand, arrival)
        end = start + stop.duration_min
        if is_walking_stop(cand):
            walking += stop.duration_min
        plan_b = ledger.get(stop.plan_b_candidate_id) if stop.plan_b_candidate_id else None
        stops.append(ScheduledStop(
            candidate=cand, start_min=start, end_min=end, duration_min=stop.duration_min,
            why=stop.why, tradeoff=stop.tradeoff, plan_b=plan_b,
        ))
        prev_end = end

    currency = plan_currency(ctx, prefs, candidates)
    for i, cand in enumerate(candidates):
        if cand.cost.currency != currency:
            violations.append(Violation(
                code="CURRENCY_MISMATCH", severity="error", stop_index=i, candidate_id=cand.id,
                message=f"{cand.name} is priced in {cand.cost.currency}, not the budget's {currency}",
            ))
    for i, leg in enumerate(legs):
        if leg.fare.currency != currency:
            violations.append(Violation(
                code="CURRENCY_MISMATCH", severity="error", stop_index=i + 1, candidate_id=leg.to_id,
                message=f"The fare to stop {i + 2} is estimated in {leg.fare.currency}, not the budget's {currency}",
            ))
    mixed = bool(foreign_currencies(currency, candidates, legs))  # CURRENCY_MISMATCH raised above

    elapsed = prev_end - ctx.start_min
    totals = compute_totals(
        candidates, legs, currency=currency, budget=prefs.budget,
        elapsed_min=elapsed, available_minutes=prefs.available_minutes, walking_min=walking, skip_foreign=mixed,
    )
    plan = CanonicalPlan(
        date=ctx.date, city=ctx.location.name, timezone=ctx.location.timezone,
        start_min=ctx.start_min, end_min=prev_end, stops=stops, legs=legs, totals=totals,
        summary_note=draft.summary_note, omitted=named_omissions(draft.omitted, ledger), weather=ctx.weather,
        maps_route_url=directions_url(candidates),
    )
    return plan, violations
