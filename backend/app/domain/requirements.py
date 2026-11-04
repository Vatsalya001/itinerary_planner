"""Conservative venue-facility requirements, independent of model-authored evidence.

Known facilities have explicit evidence patterns. Other mandatory requirements remain blockers rather
than being guessed into a supported rule. Evidence is source-backed, scoped and expiring; unknown is never yes.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.domain.schemas import (
    Candidate,
    ParseOutcome,
    PlanRequest,
    Preferences,
    VenueRequirement,
    VenueRequirementEvidence,
)

OPTIONAL_PREFIX = "Optional preference: "
EVIDENCE_TTL = timedelta(hours=6)
MAX_REQUIREMENTS = 5


@dataclass(frozen=True)
class Facility:
    label: str
    pattern: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pattern", rf"\b(?:{self.pattern})\b")


FACILITIES = (
    Facility(
        "private changing room",
        r"private (?:changing|change) rooms?|private rooms? (?:for changing|to change clothes)",
    ),
    Facility(
        "baby changing facilities",
        r"baby[- ]changing (?:facilit\w*|rooms?|stations?|tables?)|diaper[- ]changing (?:stations?|tables?)",
    ),
    Facility(
        "wheelchair-accessible toilet",
        r"(?:wheelchair[- ]accessible|accessible|disabled) (?:toilets?|restrooms?|washrooms?)",
    ),
    Facility("on-site parking", r"on[- ]site (?:car )?parking"),
    Facility("free parking", r"free (?:car )?parking|complimentary (?:car )?parking"),
    Facility("lift access", r"(?:lifts?|elevators?)(?: access)?"),
    Facility("nursing room", r"nursing rooms?|breastfeeding rooms?|lactation rooms?"),
    Facility("lockers", r"(?:secure )?lockers?"),
)
_HARD = re.compile(
    r"\b(?:must|mandatory|required|require|need|every|each|all venues|only venues|essential|cannot|can't|no |avoid )\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(?:no|not|without|unavailable|lack\w*|isn't|aren't|doesn't|don't|not provided|closed|removed)\b", re.I
)
_QUALIFIED = re.compile(
    r"\b(?:may|might|sometimes|whether|if |ask|enquire|inquire|upon request|on request|subject to|depending|temporarily|booking required|except|only for|will|would|could|planned|proposed|coming soon|for staff|for employees|for members|guests only)\b",
    re.I,
)
_AFFIRMED = re.compile(r"\b(?:available|provided|equipped|offers?|has|have|includes?)\b", re.I)
_INJECTION = re.compile(
    r"ignore .*instructions?|system prompt|assistant:|developer:|mark .*confirmed|override .*rules?", re.I
)
_BLOCKED_HOSTS = {
    "facebook.com",
    "instagram.com",
    "google.com",
    "maps.google.com",
    "youtube.com",
    "tripadvisor.com",
    "booking.com",
}


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def requirement_id(label: str, scope: str) -> str:
    return hashlib.sha256(f"{normalized(label)}|{scope}".encode()).hexdigest()[:12]


def facility_for(text: str) -> Facility | None:
    return next((f for f in FACILITIES if re.search(f.pattern, text, re.I)), None)


def official_host(url: str | None) -> str | None:
    """Validate identities only. Research never fetches these user/provider URLs directly."""
    if not url or len(url) > 2048:
        return None
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return None
        if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host or ".." in host:
            return None
        if host.endswith((".local", ".localhost", ".internal", ".test", ".invalid")):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        if any(host == h or host.endswith("." + h) for h in _BLOCKED_HOSTS):
            return None
        return host.removeprefix("www.")
    except ValueError:
        return None


def attach_requirements(outcome: ParseOutcome, request: PlanRequest) -> ParseOutcome:
    """Extract only explicit facility requirements, from user text, without another model call.

    Unknown vocabulary stays in unmapped_constraints. Compound clauses are scanned independently so a
    known vegetarian/wheelchair rule cannot swallow an additional unverified facility requirement.
    """
    prefs = outcome.preferences.model_copy(deep=True)
    entries = [(v, not v.lower().startswith(OPTIONAL_PREFIX.lower())) for v in request.constraints]
    for sentence in re.split(r"[.!?;\n]", request.free_text or ""):
        if _HARD.search(sentence):
            entries.append((sentence, True))
    found: dict[str, VenueRequirement] = {}
    matched: list[str] = []
    for text, mandatory in entries:
        if _INJECTION.search(text):
            continue
        # Negated facilities need a dedicated absence rule; never reinterpret them as presence.
        for facility in FACILITIES:
            m = re.search(facility.pattern, text, re.I)
            if m is None:
                continue
            before = text[max(0, m.start() - 30) : m.start()]
            if re.search(r"\b(?:no|not|without|avoid|don't need|do not need)\b", before, re.I):
                continue
            scope = (
                "any_stop"
                if re.search(r"at least one|one (?:stop|venue)", text, re.I)
                else "food_stops"
                if re.search(r"(?:every|each|all) (?:food stop|restaurant|cafe)", text, re.I)
                else "all_stops"
            )
            rid = requirement_id(facility.label, scope)
            optional = f"requirement:{rid}" in request.relaxed_constraints or not mandatory
            item = VenueRequirement(id=rid, description=facility.label, mandatory=not optional, scope=scope)
            previous = found.get(rid)
            if previous is None or item.mandatory:
                found[rid] = item
            matched.append(text)
    # Never truncate an unhandled mandatory requirement into a silently weakened request.
    if len(found) > MAX_REQUIREMENTS:
        return outcome.model_copy(
            update={
                "unmapped_constraints": [
                    *outcome.unmapped_constraints,
                    "Too many additional facility requirements; please use at most five",
                ]
            }
        )
    # A recognised diet/access word must not hide a different mandatory facility in the same input.
    from app.domain.constraints import map_constraint_text

    extra_unknown = []
    for text, mandatory in entries:
        if not mandatory or _INJECTION.search(text):
            continue
        for clause in re.split(r"\band\b|[,;]", text, flags=re.I):
            clause = clause.strip()
            if (
                re.search(r"\bmust (?:have|provide|offer)\b", clause, re.I)
                and not facility_for(clause)
                and not map_constraint_text(clause)
                and clause not in outcome.unmapped_constraints and clause not in extra_unknown
            ):
                extra_unknown.append(clause[:120])
    prefs.venue_requirements = list(found.values())
    handled_labels = [f for f in FACILITIES if any(re.search(f.pattern, s, re.I) for s in matched)]
    # Clear only a clause that expresses a supported facility by itself. Unknown extra clauses stay blocking.
    remaining = []
    for clause in [*outcome.unmapped_constraints, *extra_unknown]:
        if not any(re.search(f.pattern, clause, re.I) for f in handled_labels):
            remaining.append(clause)
            continue
        stripped = clause
        for f in handled_labels:
            stripped = re.sub(f.pattern, "", stripped, flags=re.I)
        stripped = re.sub(
            r"\b(?:every|each|all|venue|venues|stop|stops|must|have|has|a|an|the|and|with|be|is|are|required|mandatory|access|please|need|require|i|at|least|one|food|restaurant|restaurants|cafe|cafes)\b",
            "",
            stripped,
            flags=re.I,
        )
        if normalized(stripped):
            remaining.append(clause)
    return outcome.model_copy(update={"preferences": prefs, "unmapped_constraints": remaining})


def applies(requirement: VenueRequirement, candidate: Candidate) -> bool:
    from app.domain.evidence import is_food_stop

    return requirement.scope != "food_stops" or is_food_stop(candidate)


def assess_text(
    candidate: Candidate, requirement: VenueRequirement, url: str, text: str, city: str, now: datetime
) -> VenueRequirementEvidence:
    """Only explicit, unqualified facility statements on the venue's official domain can confirm.

    We intentionally return unknown for ambiguous prose. No LLM receives or follows page instructions.
    Search snippets are research leads; the quote must come from returned raw page text.
    """
    unknown = VenueRequirementEvidence(
        requirement_id=requirement.id,
        description=requirement.description,
        reason="No explicit official-source evidence found",
    )
    host = official_host(candidate.website_url)
    if not host or official_host(url) != host or _INJECTION.search(text):
        return unknown
    facility = facility_for(requirement.description)
    if facility is None:
        return unknown
    body = normalized(text)
    identity = normalized(candidate.name)
    if not identity or identity not in body or not re.search(rf"\b{re.escape(normalized(city))}\b", body):
        return unknown.model_copy(update={"reason": "The page does not establish this venue and city"})
    findings: list[tuple[str, str]] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        quote = sentence.strip()
        if (
            not quote
            or quote.endswith("?")
            or len(quote) > 500
            or not re.search(facility.pattern, quote, re.I)
        ):
            continue
        if _QUALIFIED.search(quote):
            continue
        if _NEGATIVE.search(quote):
            findings.append(("contradicted", quote))
        elif _AFFIRMED.search(quote):
            findings.append(("confirmed", quote))
    if not findings:
        return unknown
    statuses = {s for s, _ in findings}
    if len(statuses) != 1:
        return unknown.model_copy(update={"reason": "The page contains conflicting facility statements"})
    status, quote = findings[0]
    return VenueRequirementEvidence(
        requirement_id=requirement.id,
        description=requirement.description,
        status=status,
        source_url=url,
        retrieved_at=now,
        quote=quote,
        reason="Explicit statement on the venue's official domain",
    )


def evidence_satisfies(candidate: Candidate, requirement: VenueRequirement, city: str, now: datetime) -> bool:
    """Revalidate stored evidence, not a model's status flag. Stale/future/missing proof is unknown."""
    for e in candidate.requirement_evidence:
        if (
            e.requirement_id != requirement.id
            or e.status != "confirmed"
            or not e.source_url
            or not e.quote
            or not e.retrieved_at
        ):
            continue
        if (
            e.retrieved_at.tzinfo is None
            or now.tzinfo is None
            or not -timedelta(minutes=5) <= now - e.retrieved_at <= EVIDENCE_TTL
        ):
            continue
        # The context clock is captured before bounded retrieval; allow five minutes within that run.
        # Venue/city identity was checked against the complete research page. Re-check the source and quote here.
        host = official_host(candidate.website_url)
        f = facility_for(requirement.description)
        if (
            host
            and official_host(e.source_url) == host
            and f
            and re.search(f.pattern, e.quote, re.I)
            and _AFFIRMED.search(e.quote)
            and not _NEGATIVE.search(e.quote)
            and not _QUALIFIED.search(e.quote)
            and not _INJECTION.search(e.quote)
        ):
            return True
    return False


def requirement_problems(candidate: Candidate, prefs: Preferences, city: str, now: datetime) -> list[str]:
    return [
        f"{r.description} at {candidate.name} is not verified by a current official source"
        for r in prefs.venue_requirements
        if r.mandatory
        and r.scope != "any_stop"
        and applies(r, candidate)
        and not evidence_satisfies(candidate, r, city, now)
    ]
