"""Narrow, dated official venue policies and explicit limits of heuristic planning.

No network calls. This is a maintained policy overlay, not comprehensive admission verification.
Unknown age/accompaniment is never assumed to satisfy a known eligibility restriction.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.domain.schemas import Candidate, Preferences, WorldContext

POLICIES = Path(__file__).resolve().parents[2] / "data" / "venue_policies.json"
TICKET_CATEGORIES = frozenset({"museum", "art_gallery", "aquarium", "zoo", "historical_site"})
_SHORT_TRAVEL = re.compile(r"short.{0,15}travel|travel.{0,15}short|nearby|close together|minimi[sz]e.{0,15}travel", re.I)


@lru_cache(maxsize=1)
def venue_policies() -> list[dict[str, Any]]:
    policies: list[dict[str, Any]] = json.loads(POLICIES.read_text())
    return policies


def policy_for(c: Candidate, ctx: WorldContext) -> dict[str, Any] | None:
    name = re.sub(r"[^a-z0-9]+", " ", c.name.lower()).strip()
    for policy in venue_policies():
        if ctx.location.country_code.upper() != policy["country"]:
            continue
        if ctx.location.name.lower().strip() not in policy["cities"]:
            continue
        if any(name == alias or (policy.get("prefix") and name.startswith(alias + " ")) for alias in policy["names"]):
            return policy
    return None


def admission_problem(c: Candidate, ctx: WorldContext) -> str | None:
    policy = policy_for(c, ctx)
    if policy and policy.get("requires_child"):
        return (f"{c.name} requires an accompanying child under its published admission policy; "
                "this planner cannot verify the party's age/accompaniment eligibility")
    child_focused = c.category == "museum" and bool(re.search(
        r"\bchildren(?:'s|s)?\b.*\bmuseum\b|\bmuseum\b.*\bchildren(?:'s|s)?\b", c.name, re.I))
    if "audience:children" in c.tags or child_focused:
        return f"{c.name} is a children's venue; age and accompaniment eligibility have not been verified"
    return None


def admission_estimate_unverified(c: Candidate) -> bool:
    return (c.source.provider not in {"fixture", "fixture_event"} and (c.category in TICKET_CATEGORIES or c.cost.max == 0)
            and c.cost.basis in {"category_estimate", "price_level_estimate"})


def prefers_short_travel(prefs: Preferences) -> bool:
    return bool(_SHORT_TRAVEL.search(" ".join([prefs.mood, *prefs.soft_preferences])))
