"""Render factual plan explanations from validated records, never unchecked model prose."""
from __future__ import annotations

from app.domain.evidence import calm_at, check_constraint, is_food_stop, is_outdoor
from app.domain.realworld import admission_estimate_unverified
from app.domain.schemas import CanonicalPlan, Constraint, Energy, Preferences, WorldContext
from app.domain.scoring import covered_interests, safe_interest_label


def ground_plan_text(plan: CanonicalPlan, prefs: Preferences, constraints: list[Constraint],
                     ctx: WorldContext) -> CanonicalPlan:
    """The model selects stops; the server owns claims about their amenities and fit."""
    stops = []
    covered: set[str] = set()
    for stop in plan.stops:
        c = stop.candidate
        hits = covered_interests(c, prefs.interests)
        covered.update(hits)
        labels = [label for interest in prefs.interests if interest in hits
                  if (label := safe_interest_label(interest))]
        parts = ["Fits your interest in " + " and ".join(labels[:2]) if labels else "Adds a break between your main activities"]
        if prefs.energy == Energy.LOW and c.seated:
            parts.append("a seated break to suit your low energy")
        for con in constraints:
            if con.kind == "diet" and is_food_stop(c) and check_constraint(c, con, stop.start_min, stop.end_min, ctx).satisfied:
                parts.append(f"{con.value.capitalize()} options are supported by the place data")
        if calm_at(c, stop.start_min, stop.end_min):
            parts.append("the crowd estimate suggests a quieter visit, though this is not live crowd data")
        if ctx.weather.is_rainy and c.indoor is True:
            parts.append("an indoor option for the rainy forecast")
        tradeoffs: list[str] = []
        if admission_estimate_unverified(c):
            tradeoffs.append("Admission cost is unverified; confirm the current ticket price before booking.")
        if "admission:general_free" in c.tags:
            tradeoffs.append("General admission is free; optional paid exhibitions are not included.")
        if c.hours.status == "unknown":
            tradeoffs.append("Opening hours are unverified; check with the venue before leaving.")
        if ctx.weather.is_rainy and is_outdoor(c):
            tradeoffs.append("Use the validated indoor alternative if it rains." if stop.plan_b else
                             "This is outdoors with no verified indoor alternative; rain may make it unsuitable.")
        stops.append(stop.model_copy(update={"why": "; ".join(parts)[:299], "tradeoff": " ".join(tradeoffs) or None}))
    missed = [label for interest in prefs.interests if interest not in covered
              if (label := safe_interest_label(interest))]
    summary = "Stops were selected for your preferences and checked against the available data."
    if missed:
        summary = ("This itinerary does not cover " + ", ".join(missed[:3]) +
                   "; no suitable stop for those interests was selected within the constraints.")
    omissions = [o.model_copy(update={"reason": "Not selected for this itinerary."}) for o in plan.omitted if o.name]
    return plan.model_copy(update={"stops": stops, "summary_note": summary, "omitted": omissions})
