"""What a shared plan may store, and how it is re-hydrated for display.

Storage rules (IMPLEMENTATION_PLAN.md sections 6 and 11, eval case ``share_link_roundtrip``):

* No free text: ``preferences.mood``, ``preferences.soft_preferences`` and each hard constraint's ``source_text``
  are removed; the resume ``thread_id`` is dropped.
* Google Maps content: for candidates whose ``source.provider == "google_places"`` (stops and plan B) we keep the
  place ID, our computed fields (cost, evidence, tags, pacing fields, duration range, event fields) and the
  coordinates (the share TTL is 30 days, the limit Google allows for coordinates), and drop display content:
  name, address, neighbourhood, rating, rating count, hours, Maps URL and source URL. The redacted IDs are listed in
  the record's sidecar. Occurrences of a redacted place's name inside our own prose (why/trade-off/warnings/check
  details...) are replaced by a ``[[place:<id>]]`` token so the name is not stored there either.
* ``rehydrate`` re-fetches display fields with ``PlacesProvider.get`` when the plan is opened; if that fails the
  stop reads "Place details unavailable" and links to Google Maps by place ID.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.domain.schemas import Candidate, Energy, Location, PlanResult, WorldContext

log = logging.getLogger(__name__)

RECORD_VERSION = 1
REDACTED_NAME = ""
UNAVAILABLE_NAME = "Place details unavailable"
UNAVAILABLE_WARNING = "Some place details could not be refreshed from Google Maps; open the links for current details."
PLACE_TOKEN = "[[place:{id}]]"
_TOKEN_RE = re.compile(r"\[\[place:([A-Za-z0-9:_\-]{1,200})\]\]")

# Display fields of a Google place that must not be stored (reset to the schema default).
GOOGLE_DISPLAY_FIELDS: dict[str, Any] = {
    "name": REDACTED_NAME,
    "address": None,
    "neighbourhood": None,
    "rating": None,
    "rating_count": None,
    "hours": {"status": "unknown", "periods": []},
    "maps_url": None,
}
# Keys whose string values are our prose (never IDs or URLs); place names inside them are tokenised.
PROSE_KEYS = {"why", "tradeoff", "warnings", "summary_note", "reason", "message", "detail", "label",
              "assumptions", "degradations", "summary"}


def google_place_url(candidate_id: str) -> str:
    """Keyless Maps URL by place ID (no display content needed)."""
    place_id = candidate_id.removeprefix("gp:")
    return f"https://www.google.com/maps/search/?api=1&query=Google&query_place_id={quote(place_id, safe='')}"


def _is_google(cand: dict[str, Any]) -> bool:
    source = cand.get("source") or {}
    return source.get("provider") == "google_places" or str(cand.get("id", "")).startswith("gp:")


def _plan_candidates(result: dict[str, Any]) -> Iterable[dict[str, Any]]:
    plan = result.get("plan") or {}
    for stop in plan.get("stops") or []:
        for key in ("candidate", "plan_b"):
            cand = stop.get(key)
            if isinstance(cand, dict):
                yield cand


def _replace_in_prose(node: Any, replace: Any, key: str | None = None) -> Any:
    if isinstance(node, dict):
        return {k: _replace_in_prose(v, replace, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_in_prose(v, replace, key) for v in node]
    if isinstance(node, str) and key in PROSE_KEYS:
        return replace(node)
    return node


def for_storage(result: PlanResult) -> dict[str, Any]:
    """The share-link record: ``{"v", "result", "redacted": {"google_candidates": [...]}}``."""
    data = result.model_dump(mode="json")
    data["thread_id"] = None
    prefs = data.get("preferences")
    if isinstance(prefs, dict):
        prefs.pop("mood", None)
        prefs.pop("soft_preferences", None)
        for constraint in prefs.get("hard_constraints") or []:
            constraint.pop("source_text", None)

    if isinstance(data.get("plan"), dict):
        for omitted in data["plan"].get("omitted", []):
            if str(omitted.get("ref", "")).startswith("gp:"):
                omitted["name"] = None
                omitted["reason"] = "Another option was not selected"
    names: dict[str, str] = {}
    redacted: list[str] = []
    for cand in _plan_candidates(data):
        if not _is_google(cand):
            continue
        cid = str(cand["id"])
        if cand.get("name"):
            names[cid] = str(cand["name"])
        cand.update(copy.deepcopy(GOOGLE_DISPLAY_FIELDS))
        source = cand.get("source")
        if isinstance(source, dict):
            source["url"] = None
        if cid not in redacted:
            redacted.append(cid)

    # Longest names first so "Cafe Mocha Indiranagar" is tokenised before "Cafe Mocha".
    ordered = sorted(((n, cid) for cid, n in names.items() if len(n.strip()) >= 3), key=lambda x: -len(x[0]))
    if ordered:
        def tokenise(text: str) -> str:
            for name, cid in ordered:
                if name in text:
                    text = text.replace(name, PLACE_TOKEN.format(id=cid))
            return text

        data = _replace_in_prose(data, tokenise)
    return {"v": RECORD_VERSION, "result": data, "redacted": {"google_candidates": redacted}}


def _context_for(result: PlanResult, now: datetime) -> WorldContext | None:
    """A WorldContext good enough for ``PlacesProvider.get`` (location, date and window of the stored plan)."""
    from app.domain.pacing import profile_for

    plan = result.plan
    if plan is None or not plan.stops:
        return None
    cands = [s.candidate for s in plan.stops]
    location = Location(
        id=cands[0].city_id, name=plan.city, country_code="", timezone=plan.timezone,
        lat=sum(c.lat for c in cands) / len(cands), lng=sum(c.lng for c in cands) / len(cands),
        currency=plan.totals.currency,
    )
    energy = result.preferences.energy if result.preferences else Energy.MEDIUM
    return WorldContext(
        location=location, date=plan.date, start_min=plan.start_min, end_min=plan.end_min, weather=plan.weather,
        pacing=profile_for(energy), meal_windows=[], now=now,
    )


async def _fetch(places: Any, cid: str, ctx: WorldContext, timeout_s: float) -> Candidate | None:
    try:
        fresh = await asyncio.wait_for(places.get(cid, ctx), timeout=timeout_s)
    except Exception as exc:
        log.warning("rehydrate fetch failed", extra={"error_type": type(exc).__name__,
                                                     "code": getattr(exc, "code", None)})
        return None
    return fresh if isinstance(fresh, Candidate) and fresh.name else None


def _restore(cand: Candidate, fresh: Candidate | None) -> Candidate:
    """Copy only the redacted display fields (``GOOGLE_DISPLAY_FIELDS``) and the source URL from ``fresh``.

    Everything we computed when planning (category, cost, evidence, tags, pacing and duration fields, event
    times) stays as stored, so the plan's checks, totals and schedule still describe what was validated."""
    if fresh is None:
        return cand.model_copy(update={"name": UNAVAILABLE_NAME, "maps_url": google_place_url(cand.id)})
    source = cand.source.model_copy(update={"url": fresh.source.url, "attribution": fresh.source.attribution
                                            or cand.source.attribution})
    update: dict[str, Any] = {key: getattr(fresh, key) for key in GOOGLE_DISPLAY_FIELDS}
    update["maps_url"] = fresh.maps_url or google_place_url(cand.id)
    update["source"] = source
    return cand.model_copy(update=update)


async def rehydrate(record: dict[str, Any], places: Any, *, now: datetime | None = None,
                    timeout_s: float = 6.0) -> PlanResult:
    """Rebuild a displayable PlanResult from a stored record. ``places`` is a PlacesProvider (or None)."""
    raw = record.get("result", record) if record.get("v") is not None else record
    sidecar = (record.get("redacted") or {}) if record.get("v") is not None else {}
    redacted_ids = [str(i) for i in sidecar.get("google_candidates") or []]
    result = PlanResult.model_validate(raw)

    fresh_by_id: dict[str, Candidate | None] = dict.fromkeys(redacted_ids)
    ctx = _context_for(result, now or datetime.now(UTC)) if redacted_ids else None
    if redacted_ids and places is not None and ctx is not None:
        fetched = await asyncio.gather(*(_fetch(places, cid, ctx, timeout_s) for cid in redacted_ids))
        fresh_by_id = dict(zip(redacted_ids, fetched, strict=True))

    if result.plan is not None and redacted_ids:
        stops = []
        for stop in result.plan.stops:
            update: dict[str, Any] = {}
            if stop.candidate.id in fresh_by_id:
                update["candidate"] = _restore(stop.candidate, fresh_by_id[stop.candidate.id])
            if stop.plan_b is not None and stop.plan_b.id in fresh_by_id:
                update["plan_b"] = _restore(stop.plan_b, fresh_by_id[stop.plan_b.id])
            stops.append(stop.model_copy(update=update) if update else stop)
        result = result.model_copy(update={"plan": result.plan.model_copy(update={"stops": stops})})

    names = {cid: (fresh.name if fresh is not None else "this place") for cid, fresh in fresh_by_id.items()}

    def detokenise(text: str) -> str:
        return _TOKEN_RE.sub(lambda m: names.get(m.group(1), "this place"), text)

    data = _replace_in_prose(result.model_dump(mode="json"), detokenise)
    if any(fresh is None for fresh in fresh_by_id.values()) and UNAVAILABLE_WARNING not in data["warnings"]:
        data["warnings"].append(UNAVAILABLE_WARNING)
    return PlanResult.model_validate(data)
