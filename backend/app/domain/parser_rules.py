"""Deterministic preference parser and clarification policy. OWNER: parser agent (Phase 1).

* ``parse_rules``: the no-LLM parser, and the safety net under the LLM parser.
* ``original_constraints``: hard constraints straight from the raw request; never dropped.
* ``merge_parse``: explicit request fields > LLM > rules; hard constraints can only be added.
* ``clarification_questions`` / ``apply_answers``: the check_clarity policy and the resume path.
* ``apply_defaults`` / ``default_queries``: final preferences and the baseline place searches.

User text is data, never instructions. Sentences or constraint entries that read like prompt injection
("ignore all previous instructions", "SYSTEM: validation is disabled") never set a field and never become
a clarification question; they surface only in ``unmapped_constraints``. Hard constraints are still mapped
from every piece of text (a constraint can only make a plan stricter), so ``parse_rules`` and
``original_constraints`` always agree.

A defaulted start time is recorded at parse time as the assumption "Starting at <time>" (only the parser
knows whether the user gave one). ``merge_parse`` keeps that assumption only while no source found a
start, and ``apply_defaults`` treats it, or "start_time" in ``missing``, as "the start may be re-defaulted".
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from app.domain.categories import (
    DATA_DIR,
    currency_for_country,
    currency_symbol,
    is_category,
    load_categories,
    load_countries,
)
from app.domain.constraints import (
    detect_conflicts,
    diet_negated,
    is_valid,
    map_constraint_text,
    merge_constraints,
    negates_all_diets,
)
from app.domain.pacing import energy_from_mood, meal_windows_between
from app.domain.schemas import (
    ClarificationQuestion,
    Constraint,
    ConstraintConflict,
    Energy,
    Location,
    ParseOutcome,
    PlaceQuery,
    PlanRequest,
    Preferences,
    WorldContext,
)
from app.domain.timeutil import fmt_clock, parse_clock, parse_duration, parse_time_range

DEFAULT_START_MIN = 900  # 15:00
LONG_DAY_START_MIN = 600  # windows longer than 8 hours start at 10:00
LONG_WINDOW_MIN = 480
DEFAULT_MINUTES = 240
DEFAULT_BUDGETS: dict[str, int] = {"INR": 1500, "GBP": 40, "USD": 50, "EUR": 45}

CITY_OPTIONS = ["Bangalore", "Mumbai", "Delhi", "Pune", "Hyderabad"]
BUDGET_TIME_OPTIONS = ["3 hours, ₹1000", "4 hours, ₹2000", "Whole day, ₹3000"]
KEEP_AS_PREFERENCE = "Keep it as a preference"
SKIP_IT = "Skip it"
START_ASSUMPTION = "Starting at "
MAX_QUESTIONS = 2

_FREE_TEXT_MIN_MINUTES = 30
_FREE_TEXT_MAX_MINUTES = 720
_MAX_BUDGET = 1_000_000
_CITY_RADIUS_KM = 40.0

# ---------------------------------------------------------------------------------------------------
# Gazetteer (data/gazetteer.yaml)
# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GazPlace:
    name: str  # canonical name
    city: str  # canonical city (equals name for a city)
    country_code: str
    timezone: str
    lat: float
    lng: float
    is_city: bool


@dataclass(frozen=True)
class _Gazetteer:
    by_name: dict[str, list[GazPlace]]  # lower-cased name or alias -> places, cities first
    cities: dict[str, GazPlace]
    pattern: re.Pattern[str] | None  # case-insensitive names
    acronyms: re.Pattern[str] | None  # all-caps aliases, matched case-sensitively


def _names_pattern(names: list[str], flags: int) -> re.Pattern[str] | None:
    if not names:
        return None
    alts = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    return re.compile(rf"(?<![\w])(?:{alts})(?![\w])", flags)


def _is_acronym(name: str) -> bool:
    return name.isalpha() and name.isupper() and len(name) <= 5


@lru_cache(maxsize=1)
def _gazetteer() -> _Gazetteer:
    raw: dict[str, Any] = yaml.safe_load((DATA_DIR / "gazetteer.yaml").read_text(encoding="utf-8")) or {}
    by_name: dict[str, list[GazPlace]] = {}
    cities: dict[str, GazPlace] = {}
    names: list[str] = []
    acronyms: list[str] = []
    neighbourhoods: list[tuple[list[str], GazPlace]] = []

    def register(aliases: list[str], place: GazPlace) -> None:
        for alias in aliases:
            alias = str(alias).strip()
            if not alias:
                continue
            by_name.setdefault(alias.lower(), []).append(place)
            (acronyms if _is_acronym(alias) else names).append(alias)

    for city_name, info in (raw.get("cities") or {}).items():
        city = GazPlace(
            name=str(city_name), city=str(city_name), country_code=str(info["country_code"]).upper(),
            timezone=str(info["timezone"]), lat=float(info["lat"]), lng=float(info["lng"]), is_city=True,
        )
        cities[city.name] = city
        register([city.name, *(info.get("aliases") or [])], city)
        for nb_name, nb in (info.get("neighbourhoods") or {}).items():
            place = GazPlace(
                name=str(nb_name), city=city.name, country_code=city.country_code, timezone=city.timezone,
                lat=float(nb["lat"]), lng=float(nb["lng"]), is_city=False,
            )
            neighbourhoods.append(([place.name, *(nb.get("aliases") or [])], place))
    for aliases, place in neighbourhoods:  # after every city, so cities come first for shared names
        register(aliases, place)
    return _Gazetteer(
        by_name=by_name, cities=cities,
        pattern=_names_pattern(names, re.IGNORECASE), acronyms=_names_pattern(acronyms, 0),
    )


def lookup_place(name: str | None) -> GazPlace | None:
    """Gazetteer entry for a city or neighbourhood name or alias (cities win ties); None if unknown."""
    if not name:
        return None
    gaz = _gazetteer()
    cleaned = re.sub(r"\s+", " ", name).strip().strip(".")
    for candidate in (cleaned, cleaned.split(",")[0].strip()):
        hits = gaz.by_name.get(candidate.lower())
        if hits:
            return hits[0]
    return None


def _neighbourhood_entry(name: str, city: str | None) -> GazPlace | None:
    hits = _gazetteer().by_name.get(re.sub(r"\s+", " ", name).strip().lower(), [])
    for place in hits:
        if not place.is_city and (city is None or place.city == city):
            return place
    return None


def _find_places(text: str) -> list[tuple[int, int, list[GazPlace]]]:
    """Non-overlapping gazetteer mentions in ``text`` as (start, end, places), in text order."""
    gaz = _gazetteer()
    found: list[tuple[int, int, list[GazPlace]]] = []
    for pattern in (gaz.pattern, gaz.acronyms):
        if pattern is None:
            continue
        for m in pattern.finditer(text):
            found.append((m.start(), m.end(), gaz.by_name[m.group(0).lower()]))
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    out: list[tuple[int, int, list[GazPlace]]] = []
    for item in found:
        if out and item[0] < out[-1][1]:
            continue
        out.append(item)
    return out


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?;]+(?=\s|$)|\n+")
_CLAUSE_SPLIT = re.compile(r",|\s+(?:but|and|then|also|plus)\s+", re.IGNORECASE)


def _normalize_text(text: str | None) -> str:
    """Unicode-normalize and smooth notation that would otherwise break sentence splitting."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).replace("\u2019", "'").replace("\u2018", "'").replace("\u2212", "-")
    t = re.sub(r"(?<=[\d\s])([ap])\.\s?m\b", r"\1m", t, flags=re.IGNORECASE)  # 8 p.m. -> 8 pm.
    t = re.sub(r"\b(rs)\.(?=\s*\d)", r"\1 ", t, flags=re.IGNORECASE)  # Rs. 1500 -> Rs 1500
    t = re.sub(r"\b([A-Z])\.\s?([A-Z])\.", r"\1\2", t)  # M.G. Road -> MG Road, D.C. -> DC
    t = _dotted_times(t)
    return re.sub(r"[ \t]+", " ", t).strip()


# Dotted clock times (common Indian notation) become colon times so every time parser reads them:
# "4.30pm", "4.30 pm", "from 4.30", "4.30 to 8", "16.30". Amounts ("₹4.50", "1.5k", "2.5 hours") are untouched.
_NOT_AFTER_MONEY = r"(?<![\d.,₹$£€¥])(?<!rs )(?<!rs)"
_DOTTED_TIME_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"{_NOT_AFTER_MONEY}\b(\d{{1,2}})\.([0-5]\d)(?=\s*[ap]m\b)", re.IGNORECASE), r"\1:\2"),
    (re.compile(r"\b(from|till|until|to|after|by|before|starting|start|begin|free)\s+(\d{1,2})\.([0-5]\d)"
                r"(?![\d.,])", re.IGNORECASE), r"\1 \2:\3"),
    (re.compile(rf"{_NOT_AFTER_MONEY}\b(\d{{1,2}})\.([0-5]\d)(?=\s*(?:to|till|until|-|–)\s*\d)", re.IGNORECASE),
     r"\1:\2"),
    (re.compile(rf"{_NOT_AFTER_MONEY}\b(1[3-9]|2[0-3])\.([0-5]\d)(?![\d.,])(?!\s*(?:k\b|rupees?|rs\b|inr\b|dollars?"
                rf"|pounds?|euros?|bucks|hours?|hrs?))", re.IGNORECASE), r"\1:\2"),
]


def _dotted_times(text: str) -> str:
    for rx, repl in _DOTTED_TIME_RULES:
        text = rx.sub(repl, text)
    return text


def _sentences(text: str) -> list[str]:
    return [s.strip(" ,") for s in _SENTENCE_END.split(text) if s and s.strip(" ,")]


def _clauses(sentence: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT.split(sentence) if c and c.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


# ---------------------------------------------------------------------------------------------------
# Instruction-like text, hard-sounding text
# ---------------------------------------------------------------------------------------------------

_INSTRUCTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:(?:all|any|the|your|my|of|these|those|previous|prior"
    r"|above|earlier|system|safety)\s+)*(?:instructions?|prompts?|rules|guidelines|directions|guardrails)\b"
    r"|\b(?:system|developer|assistant|admin)\s*:"
    r"|\bsystem\s+prompt\b"
    r"|\bapi[\s_-]*keys?\b"
    r"|\bvalidation\b.{0,30}\b(?:disabled?|off|bypass\w*|skip\w*|passed|turned\s+off)\b"
    r"|\b(?:disable|bypass|skip|turn\s+off)\b.{0,20}\bvalidation\b"
    r"|\b(?:budget|price|cost)s?\s+(?:is|are)\s+(?:now\s+)?(?:unlimited|infinite|irrelevant|no\s+object)\b"
    r"|\bunlimited\s+budget\b|\bover[- ]budget\s+plan\b"
    r"|\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+(?:to\s+be|you)\b|\bjailbreak|\bdeveloper\s+mode\b"
    r"|\b(?:reveal|print|show|output|leak|repeat)\b.{0,30}\b(?:prompt|instructions|secrets?|keys?|passwords?"
    r"|tokens?)\b",
    re.IGNORECASE,
)
_HARD_RE = re.compile(
    r"\b(?:must|only|no|not|never|avoid|need|needs|needed|can'?t|cannot|won'?t|allergic|allerg(?:y|ies)"
    r"|intoleran(?:t|ce)|without|strictly|requires?|required|don'?t|mandatory|essential)\b",
    re.IGNORECASE,
)
_BENIGN_RE = re.compile(
    r"\b(?:no\s+(?:idea|clue|plans?|worries|preferences?|problem|rush|pressure)|not\s+(?:sure|really|much"
    r"|fussy|picky)|don'?t\s+(?:know|mind|care)|nothing\s+planned|only\s+(?:have|got|free|for)"
    r"|need\s+(?:ideas|suggestions|a\s+plan|plans|something))\b",
    re.IGNORECASE,
)
# Full-clause matches keep response guidance separate from venue requirements.
# Do not broadly ignore "need"/"won't": they also express real safety constraints.
_RESPONSE_GUIDANCE_RE = re.compile(
    r"(?:if\s+(?:both|all|they|these|those|that|this|it|the plan)(?:\s+activities)?\s+"
    r"(?:won't|can't|cannot|don't|do not)\s+fit"
    r"|(?:please\s+)?(?:clearly\s+)?(?:distinguish|differentiate|separate)\s+"
    r"(?:confirmed|verified)\s+(?:accessibility|information|facts|details)\s+from\s+"
    r"(?:anything|what|anything that)\s+(?:I|we)\s+need\s+to\s+(?:verify|check)"
    r"|(?:I|we)\s+need\s+you\s+to\s+(?:explain|show|state|list|describe)\s+"
    r"(?:the\s+|your\s+|any\s+)?(?:trade[- ]?offs?|assumptions|sources|costs|timings)"
    r"|(?:please\s+)?(?:don't|do not|never)\s+assume\s+(?:unknown|unverified)\s+"
    r"(?:facilities|venues|places)\s+are\s+accessible)",
    re.IGNORECASE,
)


def _is_response_guidance(text: str) -> bool:
    return bool(_RESPONSE_GUIDANCE_RE.fullmatch(_normalize_text(text).strip(" .!?")))


_SOFT_RE = re.compile(
    r"\b(?:prefer|preferably|ideally|would\s+(?:like|love)|rather|if\s+possible|nice\s+to\s+have)\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|avoid|without|skip|hate|dislike|don'?t|can'?t|cannot|won'?t|nothing|except)\b",
    re.IGNORECASE,
)


def _is_instruction_like(text: str) -> bool:
    return bool(_INSTRUCTION_RE.search(text))


# ---------------------------------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------------------------------

_SYMBOL_CURRENCY = {"us$": "USD", "s$": "SGD", "a$": "AUD", "c$": "CAD", "₹": "INR", "$": "USD", "£": "GBP",
                    "€": "EUR", "¥": "JPY", "/-": "INR"}
_WORD_CURRENCY = {
    "rs": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR", "rupaye": "INR", "rupiya": "INR",
    "usd": "USD", "dollar": "USD", "dollars": "USD",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "quid": "GBP", "sterling": "GBP",
    "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "aed": "AED", "dirham": "AED", "dirhams": "AED",
    "sgd": "SGD", "aud": "AUD", "cad": "CAD", "jpy": "JPY", "yen": "JPY",
}
_MULTIPLIERS = {"k": 1000, "thousand": 1000, "grand": 1000, "lakh": 100_000, "lakhs": 100_000,
                "lac": 100_000, "lacs": 100_000}
_NUM = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?"
_NUM_END = r"(?!\d|[.,]\d)"
_MULT = r"(?:\s*(?:k|thousand|grand|lakhs?|lacs?)(?![a-z]))"
_SYMBOLS = r"(?:us\$|s\$|a\$|c\$|₹|\$|£|€|¥)"
_CODES = r"(?:rs|inr|usd|gbp|eur|aed|sgd|aud|cad|jpy)"
_WORDS = r"(?:rupees?|rupaye|rupiya|rs|inr|usd|dollars?|bucks?|gbp|pounds?|quid|sterling|euros?|eur|aed" \
         r"|dirhams?|sgd|aud|cad|jpy|yen)"
_NOT_MONEY_UNIT = (
    r"(?!\s*(?:hours?|hrs?|h\b|minutes?|mins?|am\b|pm\b|:\d|o'?clock|people|persons?|ppl|pax|kids|children"
    r"|friends|adults|of\s+us|km|kms|kilomet|miles?|mi\b|stops?|places|years?|yrs|days?|weeks?|%|th\b|st\b"
    r"|nd\b|rd\b|-\s*\d|–\s*\d|to\s+\d|stars?))"
)
_MONEY_PREFIX = re.compile(
    rf"(?P<cur>{_SYMBOLS}|(?<![a-z]){_CODES}(?![a-z]))\s*(?P<num>(?:[+-]\s*)?{_NUM}){_NUM_END}(?P<mult>{_MULT})?",
    re.IGNORECASE)
_MONEY_SUFFIX = re.compile(
    rf"(?<![\d.,])(?P<num>(?:[+-]\s*)?{_NUM}){_NUM_END}(?P<mult>{_MULT})?\s*(?P<cur>/-|{_SYMBOLS}|(?<![a-z]){_WORDS}(?![a-z]))",
    re.IGNORECASE)
_MONEY_PER_PERSON = re.compile(
    rf"(?<![\d.,:])(?P<num>(?:[+-]\s*)?{_NUM}){_NUM_END}(?P<mult>{_MULT})?\s*(?:per\s+person|per\s+head|a\s+head|each|pp"
    rf"|/\s*(?:person|head|pp))\b",
    re.IGNORECASE)
_MONEY_CONTEXT = re.compile(
    rf"(?:\b(?P<ctx>budget(?:\s+(?:of|is|was|around|about|under|max|roughly|approx))*|got|have|spend(?:ing)?"
    rf"|around|about|approx(?:imately)?|roughly|under|below|upto|up\s+to|max(?:imum)?|within|less\s+than|with"
    rf"|cap|limit)|~)"
    rf"\s*[:=]?\s*(?P<num>(?:[+-]\s*)?{_NUM}){_NUM_END}(?P<mult>{_MULT})?{_NOT_MONEY_UNIT}",
    re.IGNORECASE)
_MONEY_CUE_AFTER = re.compile(  # "600 budget", "2500 max", "1500 all in", "2k tops"
    rf"(?<![\d.,:])(?P<num>(?:[+-]\s*)?{_NUM}){_NUM_END}(?P<mult>{_MULT})?\s*(?P<ctx>budget|max(?:imum)?|tops|all[\s-]+in"
    rf"|in\s+total|total|limit|cap)\b",
    re.IGNORECASE)
_MONEY_K = re.compile(r"(?<![\d.,])(?P<num>(?:[+-]\s*)?\d+(?:\.\d+)?)\s*(?P<mult>k)(?![a-z])(?!\s*(?:run|race|steps|km))",
                      re.IGNORECASE)
_MONEY_BARE = re.compile(rf"(?<![\d.,:])(?P<num>(?:[+-]\s*)?{_NUM}){_NUM_END}(?P<mult>{_MULT})?{_NOT_MONEY_UNIT}",
                         re.IGNORECASE)
_CURRENCY_MENTION = re.compile(
    r"\b(?:in|using|of)\s+(rupees|inr|pounds|gbp|dollars|usd|euros|eur|dirhams|aed|yen|jpy|sgd|aud|cad)\b",
    re.IGNORECASE)
_MIN_BARE_BUDGET = 20  # "around 4" is a time, not a budget


@dataclass(frozen=True)
class _Money:
    amount: int
    currency: str | None
    explicit: bool
    start: int


def _amount(num: str, mult: str | None) -> int | None:
    value = float(re.sub(r"\s+", "", num).replace(",", ""))
    if mult:
        value *= _MULTIPLIERS[mult.strip().lower()]
    if not math.isfinite(value) or abs(value) > _MAX_BUDGET:
        return None
    return round(value)


def _currency_token(token: str) -> str | None:
    t = token.strip().lower()
    return _SYMBOL_CURRENCY.get(t) or _WORD_CURRENCY.get(t)


def _extract_money(text: str, bare_ok: bool = False) -> _Money | None:
    """First budget amount in ``text``. Amounts with a named currency win over bare amounts."""
    named: list[_Money] = []
    bare: list[_Money] = []
    for rx in (_MONEY_PREFIX, _MONEY_SUFFIX):
        for m in rx.finditer(text):
            amount = _amount(m.group("num"), m.group("mult"))
            if amount is None:
                continue
            code = _currency_token(m.group("cur"))  # "bucks" names no currency
            (named if code else bare).append(_Money(amount, code, code is not None, m.start()))
    if named:
        return min(named, key=lambda x: x.start)
    for m in _MONEY_PER_PERSON.finditer(text):
        amount = _amount(m.group("num"), m.group("mult"))
        if amount is not None:
            bare.append(_Money(amount, None, False, m.start()))
    for m in [*_MONEY_CONTEXT.finditer(text), *_MONEY_CUE_AFTER.finditer(text)]:
        amount = _amount(m.group("num"), m.group("mult"))
        is_budget_word = (m.group("ctx") or "").lower().startswith("budget")
        if amount is not None and (is_budget_word or amount >= _MIN_BARE_BUDGET):
            bare.append(_Money(amount, None, False, m.start()))
    for m in _MONEY_K.finditer(text):
        amount = _amount(m.group("num"), m.group("mult"))
        if amount is not None:
            bare.append(_Money(amount, None, False, m.start()))
    if not bare and bare_ok:
        found = _MONEY_BARE.search(text)
        amount = _amount(found.group("num"), found.group("mult")) if found else None
        if found and amount is not None:
            bare.append(_Money(amount, None, False, found.start()))
    if not bare:
        return None
    best = min(bare, key=lambda x: x.start)
    mention = _CURRENCY_MENTION.search(text)
    if mention:
        code = _WORD_CURRENCY.get(mention.group(1).lower())
        if code:
            return _Money(best.amount, code, True, best.start)
    return best


def _budget_int(value: float) -> int:
    return int(value) if float(value).is_integer() else round(value)


def _currency_from_answer(text: str, city: str | None) -> str | None:
    """ISO code from a currency answer: "INR", "GBP", "₹", "pounds", "rupees (INR)", "local"."""
    low = _normalize_text(text).lower()
    for symbol in sorted(_SYMBOL_CURRENCY, key=len, reverse=True):
        if symbol in low:
            return _SYMBOL_CURRENCY[symbol]
    for word in re.findall(r"[a-z]+", low):
        if word in _WORD_CURRENCY:
            return _WORD_CURRENCY[word]
    known = {str(v["currency"]) for v in load_countries().values()}
    for word in re.findall(r"\b[a-z]{3}\b", low):
        if word.upper() in known:
            return word.upper()
    if re.search(r"\blocal\b", low):
        place = lookup_place(city)
        if place:
            return currency_for_country(place.country_code)
    return None


def _fmt_money(amount: int, currency: str | None) -> str:
    if not currency:
        return f"{amount:,}"
    return f"{currency_symbol(currency)}{amount:,}"


# ---------------------------------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------------------------------

_CLOCK_TOKEN = re.compile(
    r"(?<![\w:₹$£€.,])(\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2}|noon|midday|midnight)(?![\w:])", re.IGNORECASE)
_END_CUE = re.compile(r"\b(?:by|before|until|till|til|latest)\s*$", re.IGNORECASE)
_RANGE_CUE = re.compile(r"(?:\bto|[-–])\s*$", re.IGNORECASE)  # an end only after an earlier clock time
_START_CUE = re.compile(
    r"\b(?:from|after|starting|start|starts|begin|begins|free|out|leave|leaving|meet|meeting)(?:\s+at)?\s*$",
    re.IGNORECASE)
_AT_CUE = re.compile(r"\bat\s*$", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"\b(?:\d+|an?|one|two|three|four|five)\s+(?:hours?\s+and\s+a\s+half|and\s+a\s+half\s+hours?)\b"
    r"|\b\d+h\d{1,2}\b"
    r"|\b(?:\d+(?:\.\d+)?|half\s+an|an|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*"
    r"(?:hours?|hrs?|h)\b(?:\s*(?:and\s+)?\d+\s*(?:minutes?|mins?)\b)?"
    r"|\b\d+\s*(?:minutes?|mins?)\b",
    re.IGNORECASE)
_FUZZY_DURATIONS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(?:a\s+)?couple(?:\s+of)?\s+(?:hours?|hrs)\b", re.IGNORECASE), 120),
    (re.compile(r"\b(?:a\s+)?few\s+(?:hours?|hrs)\b", re.IGNORECASE), 180),
    (re.compile(r"\bhalf(?:\s+a)?[\s-]+day\b", re.IGNORECASE), 240),
    (re.compile(r"\b(?:full|whole|entire|all)[\s-]+day\b", re.IGNORECASE), 600),
]
_PART_OF_DAY = re.compile(r"\b(morning|afternoon|evening|tonight|night)\b", re.IGNORECASE)
PART_OF_DAY_SPANS: dict[str, tuple[int, int]] = {  # start minute, minutes
    "morning": (540, 180), "afternoon": (780, 240), "evening": (1020, 240), "tonight": (1140, 240),
    "night": (1140, 240),
}


@dataclass(frozen=True)
class _TimeInfo:
    start: int | None = None
    minutes: int | None = None
    start_assumed: bool = False  # only an end time was given; start is the default


def _extract_duration(text: str) -> int | None:
    m = _DURATION_RE.search(text)
    if m:
        minutes = parse_duration(m.group(0))
        if minutes:
            return minutes
    for rx, minutes in _FUZZY_DURATIONS:
        if rx.search(text):
            return minutes
    return None


# An explicit "am" attached to a number ("6am", "6 am", "6 a.m.") or an early-hours word means the user meant the
# early hours, so an early start is never moved to the afternoon. ("I am free" is not an am marker.)
_EARLY_HINT = re.compile(r"\d\s*a\.?\s?m\b|\b(?:morning|dawn|sunrise|midnight|night|overnight|early)\b",
                         re.IGNORECASE)
_EARLY_START_MIN = 7 * 60
_AMPM_TOKEN = re.compile(r"[ap]m|noon|midday|midnight", re.IGNORECASE)
_AMPM_ANYWHERE = re.compile(r"\d\s*[ap]m\b", re.IGNORECASE)


def _extract_time(text: str) -> _TimeInfo:
    """Start and duration from free text: "4 to 8pm", "from 4pm", "free for 3 hours", "afternoon".

    A clock time before 07:00 written without am/pm, when the text has no explicit "am" and no early-hours word,
    means the afternoon ("from 4:30 to 8" -> 16:30-20:00). Explicit "6am" / "12am" is never moved."""
    low = text.lower()
    early = bool(_EARLY_HINT.search(low))
    rng = parse_time_range(low)
    if rng:
        range_start, range_end = rng
        if not early and range_start < _EARLY_START_MIN and not _AMPM_ANYWHERE.search(low):
            range_start += 720
            range_end = range_end if range_end > range_start else range_end + 720
        return _TimeInfo(range_start, _clamp_minutes(range_end - range_start))
    start_tok: int | None = None
    end: int | None = None
    bare: list[int] = []
    for m in _CLOCK_TOKEN.finditer(low):
        value = parse_clock(m.group(1))
        if value is None:
            continue
        if not early and value < _EARLY_START_MIN and not _AMPM_TOKEN.search(m.group(1)):
            value += 720  # "from 4:30" -> 16:30
        before = low[max(0, m.start() - 20):m.start()]
        is_range_end = bool(_RANGE_CUE.search(before)) and (start_tok is not None or bool(bare))
        if _END_CUE.search(before) or is_range_end:
            end = value if end is None else end
        elif m.group(1).lower() == "midnight":
            start_tok = 0 if start_tok is None else start_tok
        elif _START_CUE.search(before):
            start_tok = value if start_tok is None else start_tok
        elif _AT_CUE.search(before):
            continue  # "dinner at 8pm" is an activity time, not the start
        else:
            bare.append(value)
    start: int | None = start_tok
    if start is None and len(bare) == 1:
        start = bare[0]
    minutes = _extract_duration(low)
    pod = _PART_OF_DAY.search(low)
    if start is not None and end is not None and minutes is None:
        minutes = end - start if end > start else end + 1440 - start
    if start is None and end is not None:
        if minutes is not None:
            return _TimeInfo(max(0, end - minutes), _clamp_minutes(minutes))
        if end - DEFAULT_START_MIN >= _FREE_TEXT_MIN_MINUTES:
            return _TimeInfo(DEFAULT_START_MIN, _clamp_minutes(end - DEFAULT_START_MIN), start_assumed=True)
    if start is None and pod:
        pod_start, pod_minutes = PART_OF_DAY_SPANS[pod.group(1).lower()]
        start = pod_start
        minutes = minutes if minutes is not None else pod_minutes
    return _TimeInfo(start, _clamp_minutes(minutes) if minutes is not None else None)


def _clamp_minutes(minutes: int) -> int:
    return max(_FREE_TEXT_MIN_MINUTES, min(_FREE_TEXT_MAX_MINUTES, int(minutes)))


def _has_time(text: str) -> bool:
    low = text.lower()
    return bool(parse_time_range(low) or _CLOCK_TOKEN.search(low) or _extract_duration(low)
                or _PART_OF_DAY.search(low))


def _structured_start(value: str | None) -> int | None:
    if not value:
        return None
    clock = parse_clock(value)
    if clock is not None:
        return clock
    info = _extract_time(_normalize_text(value))
    return None if info.start_assumed else info.start


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _fmt_span(minutes: int) -> str:
    """240 -> "4 hours", 90 -> "1 hour 30 minutes", 45 -> "45 minutes"."""
    h, m = divmod(int(minutes), 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m or not h:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    return " ".join(parts)


def _start_assumption(start_min: int) -> str:
    return f"{START_ASSUMPTION}{fmt_clock(start_min)}"


# ---------------------------------------------------------------------------------------------------
# Mood, interests, group size
# ---------------------------------------------------------------------------------------------------

_MOOD_RE = re.compile(
    r"\b(?:mood|feel|feeling|feelin|felt|vibes?|tired|exhausted|drained|sleepy|lazy|chill|chilled|chilling"
    r"|relax|relaxed|relaxing|calm|slow|low[- ]energy|rest|restful|quiet|easy|easygoing|cosy|cozy|unwind"
    r"|burnt\s?out|burned\s?out|meh|bored|stressed|energetic|energy|excited|hyped|active|pumped|adventurous"
    r"|adventure|romantic|fun|curious|restless|social|lonely|sad|happy)\b",
    re.IGNORECASE,
)

CANONICAL_INTERESTS = (
    "coffee", "books", "music", "live music", "food", "walks", "art", "museums", "history", "nature",
    "shopping", "movies", "games", "theatre", "dessert", "street food", "drinks", "nightlife", "wellness",
    "views", "events", "culture", "relax", "outdoors",
)

# Ordered: earlier, more specific patterns consume their text first ("live music" before "music").
_INTEREST_LEXICON: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(rf"\b(?:{p})\b", re.IGNORECASE), tags) for p, tags in [
        (r"live\s+music|live\s+bands?|gigs?|concerts?|open\s+mics?|jam\s+sessions?", ("live music", "music")),
        (r"street\s+food|chaat|pani\s?puri|vada\s?pav|food\s+stalls?|food\s+streets?", ("street food", "food")),
        (r"coffee\s+shops?|coffee|caf[eé]s?|espresso|lattes?|cappuccinos?", ("coffee",)),
        (r"book\s?shops?|book\s?stores?|bookstalls?|books?|reading|librar(?:y|ies)|literature", ("books",)),
        (r"desserts?|ice\s?creams?|gelato|sweets|cakes?|bakery|bakeries|pastr(?:y|ies)|chocolates?|waffles?",
         ("dessert",)),
        (r"brunch|lunch|dinner|breakfast|food|foodie|eat|eating|restaurants?|cuisine|meals?|biryani|dosas?"
         r"|snacks?", ("food",)),
        (r"music|jazz|songs?", ("music",)),
        (r"museums?", ("museums",)),
        (r"art|arts|artsy|arty|artistic|galler(?:y|ies)|exhibitions?|paintings?", ("art",)),
        (r"history|historic|historical|heritage|monuments?|palaces?|forts?|architecture", ("history",)),
        (r"hikes?|hiking|treks?|trekking", ("nature", "walks")),
        (r"walks?|walking|strolls?|strolling|wander(?:ing)?", ("walks",)),
        (r"nature|parks?|gardens?|lakes?|greenery|botanical|trees", ("nature",)),
        (r"outdoors|outdoorsy|open\s+air", ("outdoors",)),
        (r"shopping|shops?|boutiques?|markets?|flea|thrift(?:ing)?|bazaars?", ("shopping",)),
        (r"movies?|films?|cinemas?", ("movies",)),
        (r"board\s?games|games?|gaming|arcades?|bowling|escape\s+rooms?", ("games",)),
        (r"theatre|theater|drama|stand[- ]?up|comedy|improv", ("theatre",)),
        (r"drinks|bars?|pubs?|beers?|breweries|brewery|cocktails?|wine", ("drinks",)),
        (r"nightlife|clubbing|partying", ("nightlife",)),
        (r"spa|massages?|wellness", ("wellness",)),
        (r"views?|sunsets?|viewpoints?|rooftops?", ("views",)),
        (r"events?|festivals?|fairs?|workshops?", ("events",)),
        (r"culture|cultural", ("culture",)),
    ]
]


def _extract_interests(sentences: list[str]) -> list[str]:
    tags: list[str] = []
    for sentence in sentences:
        for clause in _clauses(sentence):
            work = clause
            for rx, out in _INTEREST_LEXICON:
                for m in rx.finditer(work):
                    if _NEGATION_RE.search(work[:m.start()]):
                        continue
                    tags.extend(t for t in out if t not in tags)
                work = rx.sub(lambda m: " " * len(m.group(0)), work)
    return tags


def normalize_interest(raw: str) -> list[str]:
    """Map one user interest to canonical tags ("bookshops" -> ["books"]); unknown text is kept lower-cased."""
    low = re.sub(r"[\s_]+", " ", raw).strip().lower()  # "outdoor_walk" -> "outdoor walk"
    if not low:
        return []
    if low in CANONICAL_INTERESTS:
        return [low]
    return _extract_interests([low]) or [low]


def _normalize_interests(raw: list[str]) -> list[str]:
    out: list[str] = []
    for item in raw:
        out.extend(t for t in normalize_interest(item) if t not in out)
    return out


_GROUP_NUMBER = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_GROUP_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10}
_GROUP_PLUS_ME = re.compile(
    rf"\bwith\s+(?:my\s+|our\s+|the\s+)?{_GROUP_NUMBER}\s+(?:kids|children|friends|people|others|colleagues"
    rf"|cousins|adults|guests|mates)\b", re.IGNORECASE)
_GROUP_TOTAL = re.compile(
    rf"\b(?:for|party\s+of|group\s+of|table\s+for)\s+{_GROUP_NUMBER}(?:\s+(?:people|persons|adults|pax|guests))"
    rf"|\b(?:party|group)\s+of\s+{_GROUP_NUMBER}\b|\b{_GROUP_NUMBER}\s+of\s+us\b|\bwe\s+are\s+{_GROUP_NUMBER}\b"
    rf"|\bwe'?re\s+{_GROUP_NUMBER}\b", re.IGNORECASE)
_GROUP_PAIR = re.compile(
    r"\bwith\s+(?:my\s+)?(?:partner|wife|husband|girlfriend|boyfriend|gf|bf|spouse|fianc[eé]e?|friend|bestie"
    r"|mom|mum|dad|mother|father|sister|brother|date)\b|\bdate\s+night\b|\bon\s+a\s+date\b|\bas\s+a\s+couple\b",
    re.IGNORECASE)


def _group_number(token: str) -> int | None:
    t = token.lower()
    if t.isdigit():
        return int(t)
    return _GROUP_WORDS.get(t)


def _extract_group(text: str) -> int | None:
    m = _GROUP_PLUS_ME.search(text)
    if m:
        n = _group_number(m.group(1))
        if n:
            return min(20, n + 1)
    m = _GROUP_TOTAL.search(text)
    if m:
        token = next(g for g in m.groups() if g)
        n = _group_number(token)
        if n and n >= 1:
            return min(20, n)
    if _GROUP_PAIR.search(text):
        return 2
    return None


# ---------------------------------------------------------------------------------------------------
# City and neighbourhood
# ---------------------------------------------------------------------------------------------------

_PRECEDED_BY_IN = re.compile(r"\b(?:in|at|around|near|visiting|exploring)\s+$", re.IGNORECASE)
_IN_PLACE_EXPLICIT = re.compile(
    r"(?i:\b(?:i'?m|i\s+am|we'?re|we\s+are|staying|based|living|currently)\s+in\s+)"
    r"([A-Za-z][\w'-]*(?:\s+[A-Z][\w'-]*){0,2})")
_IN_PLACE_CAPITALISED = re.compile(
    r"(?<![\w])(?i:in|visiting|exploring)\s+([A-Z][A-Za-z'-]+(?:\s+[A-Z][A-Za-z'-]+){0,2})")
_NOT_A_CITY = {
    "the", "a", "an", "my", "our", "your", "this", "that", "next", "love", "need", "mood", "town", "city",
    "here", "there", "home", "bed", "time", "advance", "general", "particular", "person", "between", "about",
    "search", "charge", "mind", "case", "fact", "place", "places", "front", "touch", "total", "budget", "cash",
    "rupees", "india", "england", "britain", "uk", "usa", "us", "america", "europe", "english", "hindi", "i",
    "it", "me", "rush", "morning", "afternoon", "evening", "night", "saturday", "sunday", "monday", "tuesday",
    "wednesday", "thursday", "friday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december", "ignore", "system", "least", "most", "all",
    "some", "any", "one", "two", "for", "on", "with",
}


def _unknown_city(text: str) -> str | None:
    """A city named with "I'm in X" / "in X" that the gazetteer doesn't know (the geocoder decides)."""
    for m in _IN_PLACE_EXPLICIT.finditer(text):
        name = m.group(1).strip()
        if name.split()[0].lower() in _NOT_A_CITY:
            continue
        if name[0].isupper() or re.match(r"\s*(?:[,.;!?]|$)", text[m.end():]):
            return name
    for m in _IN_PLACE_CAPITALISED.finditer(text):
        name = m.group(1).strip()
        if name.split()[0].lower() not in _NOT_A_CITY:
            return name
    return None


def _canonical_city(name: str | None) -> tuple[str | None, str | None]:
    """(city, neighbourhood) for a structured or model-provided city name."""
    if not name or not name.strip():
        return None, None
    place = lookup_place(name)
    if place is None:
        return name.strip(), None
    if place.is_city:
        return place.name, None
    return place.city, place.name


def _city_and_neighbourhood(request: PlanRequest, text: str) -> tuple[str | None, str | None]:
    city, neighbourhood = _canonical_city(request.city)
    if request.neighbourhood:
        entry = _neighbourhood_entry(request.neighbourhood, city) or _neighbourhood_entry(request.neighbourhood, None)
        if entry is not None and (city is None or entry.city == city):
            neighbourhood = entry.name
            city = city or entry.city
        else:
            neighbourhood = request.neighbourhood.strip()
    mentions = _find_places(text)
    if city is None and mentions:
        ranked = sorted(
            mentions,
            key=lambda mt: (0 if _PRECEDED_BY_IN.search(text[max(0, mt[0] - 15):mt[0]]) else 1, mt[0]),
        )
        place = ranked[0][2][0]
        city = place.city
        if not place.is_city and neighbourhood is None:
            neighbourhood = place.name
    if city is None:
        city = _unknown_city(text)
    if neighbourhood is None and city is not None:
        for _, _, places in mentions:
            hit = next((p for p in places if not p.is_city and p.city == city), None)
            if hit is not None:
                neighbourhood = hit.name
                break
    return city, neighbourhood


def _blank_places(text: str) -> str:
    out = text
    for start, end, _ in _find_places(text):
        out = out[:start] + " " * (end - start) + out[end:]
    return out


# ---------------------------------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------------------------------


def original_constraints(request: PlanRequest) -> list[Constraint]:
    """Constraints mapped directly from the raw request (fields + free text). Enforced even if a parser
    (LLM or rules) drops them. Keys in ``request.relaxed_constraints`` (dropped by the user via a one-tap
    relaxation) are excluded from every source."""
    groups = [map_constraint_text(entry) for entry in request.constraints]
    groups += [_map_sentence(s) for s in _sentences(_normalize_text(request.free_text))]
    return _without_relaxed(merge_constraints(*groups), request)


def _without_relaxed(constraints: list[Constraint], request: PlanRequest) -> list[Constraint]:
    relaxed = set(request.relaxed_constraints)
    return [c for c in constraints if c.key not in relaxed] if relaxed else list(constraints)


def _relaxed_assumptions(request: PlanRequest) -> list[str]:
    """"Dropped: wheelchair accessible (as you chose)" for each relaxed constraint key."""
    out: list[str] = []
    for key in request.relaxed_constraints:
        kind, _, value = key.partition(":")
        label = constraint_label(Constraint.model_validate({"kind": kind, "value": value}))
        out.append(f"Dropped: {label[0].lower()}{label[1:]} (as you chose)")
    return out


def _map_sentence(sentence: str) -> list[Constraint]:
    """map_constraint_text over a free-text sentence, with source_text narrowed to the triggering clause."""
    found = map_constraint_text(sentence)
    if not found:
        return found
    clauses = _clauses(sentence)
    out: list[Constraint] = []
    for c in found:
        clause = next((cl for cl in clauses if c.key in {x.key for x in map_constraint_text(cl)}), sentence)
        out.append(c.model_copy(update={"source_text": clause}))
    return out


def _clause_consumed(clause: str) -> bool:
    """True when a clause already fed another field (place, money, time, mood, interest, group)."""
    return bool(
        _find_places(clause) or _extract_money(clause) or _has_time(clause) or _MOOD_RE.search(clause)
        or _extract_interests([clause]) or _extract_group(clause)
    )


NO_DIET_RESTRICTION = "No diet restriction"


def _diet_negation_note(text: str) -> str | None:
    """A statement that only negates a diet ("I'm not vegetarian", "I eat meat") is a disclosed preference, never
    a hard constraint or a clarification."""
    if negates_all_diets(text):
        return NO_DIET_RESTRICTION
    if re.search(r"\bno (?:wheelchair|step[- ]free)(?: access)? (?:needed|required)\b", text, re.I):
        return text.strip()[:120]
    if any(diet_negated(text, diet) for diet in ("vegan", "jain", "vegetarian")):
        return text.strip()[:120]
    return None


def _unmapped_and_soft(request: PlanRequest) -> tuple[list[str], list[str]]:
    """Hard-sounding text that maps to no vocabulary constraint, and disclosed soft preferences."""
    unmapped: list[str] = []
    soft: list[str] = []
    for entry in request.constraints:
        if map_constraint_text(entry) or _is_response_guidance(entry):
            continue
        diet_note = _diet_negation_note(entry)
        if diet_note and not _is_instruction_like(entry):
            soft.append(diet_note)
        elif _is_instruction_like(entry) or _HARD_RE.search(entry):
            unmapped.append(entry)
        else:
            soft.append(entry)
    for sentence in _sentences(_normalize_text(request.free_text)):
        if _is_instruction_like(sentence):
            unmapped.append(sentence[:120])
            continue
        for clause in _clauses(sentence):
            if map_constraint_text(clause) or _BENIGN_RE.search(clause) or _is_response_guidance(clause):
                continue
            diet_note = _diet_negation_note(clause)
            if diet_note:
                soft.append(diet_note)
            elif _HARD_RE.search(clause) and not _clause_consumed(clause):
                unmapped.append(clause[:120])
            elif _SOFT_RE.search(clause) and not _clause_consumed(clause):
                soft.append(clause[:120])
    return _dedupe(unmapped), _dedupe(soft)


_CONSTRAINT_LABELS = {
    "diet:vegetarian": "Vegetarian", "diet:vegan": "Vegan", "diet:jain": "Jain food",
    "alcohol:avoid": "No alcohol venues", "access:wheelchair": "Wheelchair accessible",
    "crowd:avoid": "Avoid crowded places", "setting:indoors_only": "Indoors only",
    "setting:outdoors_only": "Outdoors only", "must_include:sit_down_meal": "Include a sit-down meal",
    "must_include:outdoor_walk": "Include an outdoor walk", "must_include:live_music": "Include live music",
}


def constraint_label(c: Constraint) -> str:
    """Short human label for a vocabulary constraint."""
    if c.key in _CONSTRAINT_LABELS:
        return _CONSTRAINT_LABELS[c.key]
    value = c.value.replace("_", " ")
    if c.kind == "must_include":
        return f"Include {value}"
    if c.kind == "must_avoid":
        return f"Avoid {value}"
    return f"{c.kind.replace('_', ' ').capitalize()}: {value}"


def _conflict_options(conflict: ConstraintConflict) -> tuple[str, str]:
    a = conflict.a.source_text.strip() or constraint_label(conflict.a)
    b = conflict.b.source_text.strip() or constraint_label(conflict.b)
    if a.lower() == b.lower():  # both came from the same sentence
        return constraint_label(conflict.a), constraint_label(conflict.b)
    return a, b


# ---------------------------------------------------------------------------------------------------
# Unread amounts, date bounds, order hints
# ---------------------------------------------------------------------------------------------------

_TIME_RANGE_SPAN = re.compile(
    r"(?:from\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:to|till|until|-|–)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?",
    re.IGNORECASE)
_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
           r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE_SPAN = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b(?:,?\s+\d{{4}})?"
    rf"|\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?"
    rf"|\b\d{{1,4}}[/-]\d{{1,2}}[/-]\d{{1,4}}\b",
    re.IGNORECASE)
_CANDIDATE_AMOUNT = re.compile(
    rf"(?<![\w.,:/#₹$£€¥-])(?P<num>(?:[+-]\s*)?{_NUM}){_NUM_END}(?P<mult>{_MULT})?{_NOT_MONEY_UNIT}"
    r"(?!\s*(?:feet|ft|road|rd|main|cross|block|sector|street|lane|floor|phase|stage|layout|nagar)\b)",
    re.IGNORECASE)
_READ_SPANS: list[re.Pattern[str]] = [
    _MONEY_PREFIX, _MONEY_SUFFIX, _MONEY_PER_PERSON, _MONEY_CONTEXT, _MONEY_CUE_AFTER, _MONEY_K, _TIME_RANGE_SPAN,
    _CLOCK_TOKEN, _DURATION_RE, *(rx for rx, _ in _FUZZY_DURATIONS), _GROUP_PLUS_ME, _GROUP_TOTAL, _DATE_SPAN,
]
_MIN_UNREAD_AMOUNT = 50
NEGATIVE_BUDGET_NOTE = "Your budget was negative; using zero until you provide a non-negative budget"
UNREAD_AMOUNT_PREFIX = "Couldn't tell whether "


_LOOSE_CUE_BEFORE = re.compile(
    r"\b(?:like|maybe|say|about|around|roughly|approx(?:imately)?|spend|spending|budget(?:\s+is)?)\s*$", re.IGNORECASE)
_LOOSE_CUE_AFTER = re.compile(r"^\s*(?:or\s+so|-?ish|odd|or\s+thereabouts)\b", re.IGNORECASE)
LOOSE_AMOUNT_PREFIX = "Read "


def _unread_amounts(text: str) -> list[tuple[int, re.Match[str], str]]:
    """Plausible money amounts that no extractor attributed, as (amount, match, text searched)."""
    work = _blank_places(text)
    for rx in _READ_SPANS:
        work = rx.sub(lambda m: " " * len(m.group(0)), work)
    found: list[tuple[int, re.Match[str], str]] = []
    for m in _CANDIDATE_AMOUNT.finditer(work):
        amount = _amount(m.group("num"), m.group("mult"))
        if amount is not None and amount >= _MIN_UNREAD_AMOUNT:
            found.append((amount, m, work))
    return found


def _unread_amount(text: str) -> str | None:
    """A plausible money amount that no extractor attributed ("600 for us"); None if there is none."""
    found = _unread_amounts(text)
    return found[0][1].group(0).strip() if found else None


def _loose_amount(text: str) -> tuple[int, str] | None:
    """"spend like 600 or so", "about 800-ish": the budget, when it is the only money-like number in the text."""
    found = _unread_amounts(text)
    if len(found) != 1:
        return None
    amount, m, work = found[0]
    if _LOOSE_CUE_BEFORE.search(work[:m.start()]) or _LOOSE_CUE_AFTER.search(work[m.end():]):
        return amount, m.group(0).strip()
    return None


MAX_DAYS_AHEAD = 60
DATE_PASSED = "That date has passed; planning for the coming Saturday"
DATE_TOO_FAR = f"That date is more than {MAX_DAYS_AHEAD} days away; planning for the coming Saturday"


def _usable_date(day: Date | None, city: str | None, now: datetime) -> tuple[Date | None, str | None]:
    """The requested date if it is today..today+60 days in the city's timezone, else (None, assumption)."""
    if day is None:
        return None, None
    place = lookup_place(city)
    local = now.astimezone(ZoneInfo(place.timezone)) if place is not None and now.tzinfo else now
    today = local.date()
    if day < today:
        return None, DATE_PASSED
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        return None, DATE_TOO_FAR
    return day, None


_ORDER_SPLIT = re.compile(r"\s*,?\s*\b(?:and\s+then|then|followed\s+by|after\s+that|afterwards)\b\s*", re.IGNORECASE)
_MEAL_WORD = re.compile(r"breakfast|brunch|lunch|dinner", re.IGNORECASE)
ORDER_PREFIX = "order: "


def _first_activity(clause: str) -> str | None:
    """Earliest activity in a clause: a meal word (brunch, dinner) or a canonical interest tag."""
    best: tuple[int, str, tuple[str, ...]] | None = None
    for rx, tags in _INTEREST_LEXICON:
        m = rx.search(clause)
        if m and not _NEGATION_RE.search(clause[:m.start()]) and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0).lower(), tags)
    if best is None:
        return None
    return best[1] if _MEAL_WORD.fullmatch(best[1]) else best[2][0]


def _order_hint(sentences: list[str]) -> str | None:
    """Stated order ("late brunch then something artsy") as "order: brunch > art"; None when not stated."""
    for sentence in sentences:
        parts = _ORDER_SPLIT.split(sentence)
        if len(parts) < 2:
            continue
        items: list[str] = []
        for i, part in enumerate(parts):
            clauses = [c.strip() for c in re.split(r"[,;]", _blank_places(part)) if c.strip()]
            if not clauses:
                continue
            item = _first_activity(clauses[-1] if i == 0 else clauses[0])
            if item and item not in items:
                items.append(item)
        if len(items) >= 2:
            return ORDER_PREFIX + " > ".join(items)
    return None


# ---------------------------------------------------------------------------------------------------
# parse_rules
# ---------------------------------------------------------------------------------------------------


def parse_rules(request: PlanRequest, now: datetime) -> ParseOutcome:
    """Regex/rule parser over free_text + structured fields. Structured fields win over free text.

    ``now`` bounds ``request.date``: a past date or one more than 60 days ahead is ignored (the coming
    Saturday is planned instead) and the reason is recorded in preferences.assumptions.
    """
    text = _normalize_text(request.free_text)
    sentences = [s for s in _sentences(text) if not _is_instruction_like(s)]
    clean = ". ".join(sentences)

    city, neighbourhood = _city_and_neighbourhood(request, clean)

    # Budget and currency: structured budget stays exactly as given.
    explicit = request.currency is not None
    currency = request.currency
    money = _extract_money(clean)
    budget: int | None = None
    if request.budget is not None:
        budget = _budget_int(request.budget)
        if not explicit and money and money.explicit and money.amount == budget:
            currency, explicit = money.currency, True
    elif money is not None:
        budget = max(0, money.amount)
        if not explicit and money.explicit:
            currency, explicit = money.currency, True

    # Time window
    found = _extract_time(clean)
    start = _structured_start(request.start_time)
    start_assumed = False
    if start is None and found.start is not None:
        start, start_assumed = found.start, found.start_assumed
    minutes = parse_duration(request.available_time) if request.available_time is not None else None
    if minutes is not None:
        minutes = _clamp_minutes(minutes)
    if minutes is None:
        minutes = found.minutes
    assumptions: list[str] = []
    if request.budget is None and money is not None and money.amount < 0:
        assumptions.append(NEGATIVE_BUDGET_NOTE)
    if start is None:
        start = LONG_DAY_START_MIN if minutes and minutes > LONG_WINDOW_MIN else DEFAULT_START_MIN
        start_assumed = True
    if start_assumed:
        assumptions.append(_start_assumption(start))

    mood = (request.mood or "").strip() or _extract_mood(sentences)
    interests = (_normalize_interests(request.interests) if request.interests
                 else _extract_interests([_blank_places(s) for s in sentences]))
    group = request.group_size if request.group_size != 1 else (
        _extract_group(" ".join([clean, request.mood or ""])) or 1)

    hard = original_constraints(request)
    assumptions.extend(_relaxed_assumptions(request))
    unmapped, soft = _unmapped_and_soft(request)
    order = _order_hint(sentences)
    if order:
        soft.append(order)

    if budget is None:
        loose = _loose_amount(clean)
        unread = None if loose else _unread_amount(clean)
        if loose:
            budget = loose[0]
            assumptions.append(f"{LOOSE_AMOUNT_PREFIX}“{loose[1]}” as your budget per person")
        elif unread:  # never silently replace a number the user wrote with a default budget
            assumptions.append(f"{UNREAD_AMOUNT_PREFIX}“{unread}” is your budget, so it wasn't used")
    day, date_note = _usable_date(request.date, city, now)
    if date_note:
        assumptions.append(date_note)

    missing = [name for name, absent in (("city", city is None), ("budget", budget is None),
                                         ("available_time", minutes is None)) if absent]
    prefs = Preferences(
        city=city, budget=budget, currency=currency, start_min=start,
        available_minutes=minutes if minutes is not None else DEFAULT_MINUTES, date=day, mood=mood,
        energy=energy_from_mood(mood) if mood else Energy.MEDIUM, interests=interests, hard_constraints=hard,
        soft_preferences=soft, neighbourhood=neighbourhood, group_size=group, assumptions=assumptions,
    )
    return ParseOutcome(
        preferences=prefs, missing=missing, unmapped_constraints=unmapped, conflicts=detect_conflicts(hard),
        budget_currency_explicit=explicit, source="rules",
    )


def _extract_mood(sentences: list[str]) -> str:
    """The user's own descriptive words ("Chill mood", "feeling okay"), joined; at most 300 characters."""
    parts: list[str] = []
    for sentence in sentences:
        for clause in re.split(r"[,;]", sentence):
            clause = clause.strip()
            if clause and _MOOD_RE.search(clause) and clause not in parts:
                parts.append(clause)
    return ", ".join(parts)[:300]


# ---------------------------------------------------------------------------------------------------
# merge_parse
# ---------------------------------------------------------------------------------------------------


def merge_parse(llm: ParseOutcome | None, rules: ParseOutcome, request: PlanRequest) -> ParseOutcome:
    """Combine an LLM parse with the rules parse: explicit request fields win, then LLM, then rules.
    Hard constraints are the union. Defaults applied are recorded in preferences.assumptions."""
    r = rules.preferences
    rules_start_found = not any(a.startswith(START_ASSUMPTION) for a in r.assumptions)
    rules_minutes_found = "available_time" not in rules.missing
    if llm is None:
        hard = _without_relaxed(merge_constraints(r.hard_constraints, original_constraints(request)), request)
        return rules.model_copy(update={
            "preferences": r.model_copy(update={"hard_constraints": hard}),
            "conflicts": detect_conflicts(hard),
            "missing": _missing(r.city, r.budget, rules_minutes_found),
            "source": "rules",
        }, deep=True)

    m = llm.preferences
    llm_city, llm_nb = _canonical_city(m.city)
    request_city_given = bool(request.city)
    city = r.city if request_city_given else (llm_city or r.city)
    if request.neighbourhood or (request_city_given and r.neighbourhood):
        neighbourhood = r.neighbourhood
    else:
        neighbourhood = m.neighbourhood or llm_nb or r.neighbourhood

    # The model's time and budget fields count only when the user's text actually contains a time expression
    # or a money-like number; otherwise they were invented and the rules value or a disclosed default is used.
    time_seen, money_seen = _text_evidence(request)
    budget = (m.budget if request.budget is None and m.budget is not None and money_seen
              and NEGATIVE_BUDGET_NOTE not in r.assumptions else r.budget)
    explicit = request.currency is not None or rules.budget_currency_explicit  # only text we can see
    if request.currency is not None:
        currency: str | None = request.currency
    elif rules.budget_currency_explicit:
        currency = r.currency
    else:
        currency = (m.currency if money_seen else None) or r.currency

    llm_minutes_found = "available_time" not in llm.missing
    if request.available_time is not None and parse_duration(request.available_time) is not None:
        minutes, minutes_found = r.available_minutes, True
    elif time_seen and llm_minutes_found and m.available_minutes != DEFAULT_MINUTES:  # default-valued == not found
        minutes, minutes_found = m.available_minutes, True
    elif rules_minutes_found:
        minutes, minutes_found = r.available_minutes, True
    else:
        minutes, minutes_found = DEFAULT_MINUTES, False

    if _structured_start(request.start_time) is not None:
        start, start_found = r.start_min, True
    elif time_seen and m.start_min != DEFAULT_START_MIN:  # a default-valued LLM start counts as "not found"
        start, start_found = m.start_min, True
    elif rules_start_found:
        start, start_found = r.start_min, True
    else:
        start_found = False
        start = LONG_DAY_START_MIN if minutes_found and minutes > LONG_WINDOW_MIN else DEFAULT_START_MIN

    mood = r.mood if (request.mood or "").strip() else (m.mood.strip() or r.mood)
    if request.interests:
        interests = r.interests
    else:  # the model may only name interests the user's own words support
        interests = _grounded_interests(m.interests, r.interests, request) or r.interests
    llm_hard = _supported_llm_constraints(m.hard_constraints, request)
    hard = _without_relaxed(merge_constraints(r.hard_constraints, original_constraints(request), llm_hard), request)
    if request.group_size != 1:
        group = request.group_size
    else:
        group = m.group_size if m.group_size != 1 else r.group_size

    assumptions = [a for a in _dedupe([*r.assumptions, *m.assumptions]) if not a.startswith(START_ASSUMPTION)
                   and not (budget is not None and a.startswith(UNREAD_AMOUNT_PREFIX))]
    if not start_found:
        assumptions.insert(0, _start_assumption(start))

    prefs = Preferences(
        city=city, budget=budget, currency=currency, start_min=start, available_minutes=minutes,
        date=r.date if request.date is not None else (m.date or r.date), mood=mood, energy=m.energy,
        interests=interests,
        hard_constraints=hard, soft_preferences=_dedupe([*r.soft_preferences, *m.soft_preferences]),
        neighbourhood=neighbourhood, group_size=group, assumptions=assumptions,
    )
    return ParseOutcome(
        preferences=prefs, missing=_missing(city, budget, minutes_found),
        unmapped_constraints=[u for u in _dedupe([*rules.unmapped_constraints, *llm.unmapped_constraints])
                              if not _is_response_guidance(u)
                              and not any(_clause_supports(c, u) for c in llm_hard)],  # the model mapped it
        conflicts=detect_conflicts(hard), budget_currency_explicit=explicit, source="llm",
    )


# Textual support for constraints a model adds. A model constraint is kept only when the user's own words (free text
# or structured constraints, never instruction-like sentences) back it: either the rules mapping already yields it,
# or this per-key lexicon matches without negation. Unsupported model constraints are dropped silently.
_LLM_NEGATED_BEFORE = re.compile(r"\b(?:no|not|don'?t|doesn'?t|never|without)\b[^,;]{0,20}$", re.IGNORECASE)
_INCLUDE_CUE = re.compile(r"\b(?:include|including|must|want|wanna|need|have\s+to|has\s+to|make\s+sure|definitely"
                          r"|should\s+have|would\s+love|really\s+want|insist)\b", re.IGNORECASE)
_AVOID_CUE = re.compile(r"\b(?:no|avoid|skip|not|without|hate|don'?t\s+want|nothing|except|away\s+from|stay\s+away)\b",
                        re.IGNORECASE)
_SUPPORT_POSITIVE: dict[str, str] = {  # key -> words that support it when not negated
    "diet:vegetarian": r"(?<!non[- ])(?<!non)\b(?:pure[- ]?)?veg(?:etarian|gie)?\b|\bno\s+(?:meat|non[- ]?veg)\b"
                       r"|\bmeat[- ]free\b",
    "diet:vegan": r"\bvegan\b|\bplant[- ]based\b|\bdairy[- ]free\b|\bno\s+dairy\b",
    "diet:jain": r"\bjain\b",
    "access:wheelchair": r"\bwheel\s?chair|\bstep[- ]free\b|\bmobility\b|\bdisab(?:led|ility)\b|\bwalker\b|\bcrutches\b",
    "crowd:avoid": r"\buncrowded\b|\bquiet\s+(?:place|spot|area)s?\b|\bpeaceful\b|\bcalm\s+(?:place|spot)s?\b",
    "setting:indoors_only": r"\bindoors?\s+only\b|\bonly\s+indoors?\b|\binside\s+only\b|\bstay(?:ing)?\s+(?:in|inside"
                            r"|indoors)\b|\bjust\s+indoors?\b|\bnothing\s+outdoors?\b|\bno\s+outdoor",
    "setting:outdoors_only": r"\boutdoors?\s+only\b|\bonly\s+outdoors?\b|\boutside\s+only\b|\bjust\s+outdoors?\b"
                             r"|\bnothing\s+indoors?\b|\bno\s+indoor|\bopen[- ]air\s+only\b",
    "alcohol:avoid": r"\bsober\b|\bteetotal(?:er|ler)?\b|\balcohol[- ]free\b|\bdon'?t\s+drink\b|\bno\s+(?:alcohol"
                     r"|drinks|booze)\b",
}
_SUPPORT_AVOID_WORDS: dict[str, str] = {  # key -> words that support it after an avoid cue in the same clause
    "crowd:avoid": r"\bcrowd(?:s|ed)?\b|\bbusy\b|\bpacked\b|\brush\b",
    "alcohol:avoid": r"\balcohol\b|\bdrinks?\b|\bdrinking\b|\bbooze\b|\bbars?\b|\bpubs?\b",
    "access:wheelchair": r"\bstairs\b|\bsteps\b",
}
_TARGET_WORDS: dict[str, str] = {  # must_include / must_avoid value -> words for it
    "sit_down_meal": r"\bsit[- ]down\b|\bmeal\b|\blunch\b|\bdinner\b|\brestaurant\b",
    "outdoor_walk": r"\bwalks?\b|\bstroll\b", "live_music": r"\blive\s+music\b|\bgigs?\b|\bconcerts?\b|\bbands?\b",
    "coffee": r"\bcoffee\b|\bcaf[eé]s?\b", "museum": r"\bmuseums?\b",
    "art": r"\bart(?:s|sy|y|istic)?\b|\bgaller(?:y|ies)\b|\bexhibitions?\b", "books": r"\bbook",
    "park": r"\bparks?\b|\bgardens?\b", "event": r"\bevents?\b|\bshows?\b|\bfestivals?\b|\bconcerts?\b",
    "shopping": r"\bshop(?:s|ping)?\b|\bmarkets?\b|\bmalls?\b", "dessert": r"\bdesserts?\b|\bice\s?creams?\b|\bsweets\b",
    "mall": r"\bmalls?\b", "bar": r"\bbars?\b|\bpubs?\b", "brewery": r"\bbrewer(?:y|ies)\b|\bmicrobrewer",
    "market": r"\bmarkets?\b|\bbazaars?\b", "cinema": r"\bcinemas?\b|\bmovies?\b|\bfilms?\b",
    "alcohol": r"\balcohol\b|\bdrinks?\b|\bbooze\b",
}


def _user_constraint_texts(request: PlanRequest) -> tuple[list[str], list[str]]:
    """(sources, clauses): structured constraint entries and free-text sentences, and their clauses. Instruction-like
    text is excluded."""
    sources = [e for e in request.constraints if not _is_instruction_like(e)]
    sources += [s for s in _sentences(_normalize_text(request.free_text)) if not _is_instruction_like(s)]
    clauses = [c.strip() for source in sources
               for c in re.split(r"[,;]|\s+(?:but|then|also|plus)\s+", source, flags=re.IGNORECASE) if c.strip()]
    return sources, clauses


def _clause_supports(c: Constraint, clause: str) -> bool:
    low = clause.lower()
    if c.key in {x.key for x in map_constraint_text(clause)}:
        return True
    if c.kind == "diet" and diet_negated(low, c.value):
        return False
    positive = _SUPPORT_POSITIVE.get(c.key)
    if positive:
        for m in re.finditer(positive, low):
            if c.kind == "diet" or not _LLM_NEGATED_BEFORE.search(low[:m.start()]):
                return True
    avoid_words = _SUPPORT_AVOID_WORDS.get(c.key)
    if avoid_words:
        for m in re.finditer(avoid_words, low):
            if _AVOID_CUE.search(low[:m.start()]):
                return True
    if c.kind in ("must_include", "must_avoid") and c.value in _TARGET_WORDS:
        cue = _INCLUDE_CUE if c.kind == "must_include" else _AVOID_CUE
        for m in re.finditer(_TARGET_WORDS[c.value], low):
            before = low[:m.start()]
            if cue.search(before) and (c.kind == "must_avoid" or not _LLM_NEGATED_BEFORE.search(before)):
                return True
    return False


def _supported_llm_constraints(constraints: list[Constraint], request: PlanRequest) -> list[Constraint]:
    """Model-proposed hard constraints that the user's own words support: the rules mapping of a whole sentence or
    entry yields it, or the per-key lexicon matches inside one clause (cues never reach across clauses)."""
    sources, clauses = _user_constraint_texts(request)
    mapped = {x.key for source in sources for x in map_constraint_text(source)}
    return [c for c in constraints
            if is_valid(c) and (c.key in mapped or any(_clause_supports(c, clause) for clause in clauses))]


def _grounded_interests(llm_interests: list[str], rules_interests: list[str], request: PlanRequest) -> list[str]:
    """LLM interests kept only when the rules parser found them or a word for them appears (un-negated) in the
    free text; invented interests ("shopping" for "plan something fun") are dropped."""
    sentences = [s for s in _sentences(_normalize_text(request.free_text)) if not _is_instruction_like(s)]
    text_tags = set(_extract_interests(sentences)) | set(rules_interests)
    low = " ".join(sentences).lower()
    out: list[str] = []
    for tag in _normalize_interests(llm_interests):
        if tag in CANONICAL_INTERESTS:
            grounded = tag in text_tags
        else:  # free-form interest ("pottery"): its word must appear in the text
            stem = re.escape(tag.rstrip("s"))
            grounded = bool(stem) and re.search(rf"\b{stem}", low) is not None
        if grounded and tag not in out:
            out.append(tag)
    return out


def _text_evidence(request: PlanRequest) -> tuple[bool, bool]:
    """(time expression seen, money-like number seen) in the free text, ignoring instruction-like sentences."""
    clean = ". ".join(s for s in _sentences(_normalize_text(request.free_text)) if not _is_instruction_like(s))
    if not clean:
        return False, False
    return _has_time(clean), bool(_extract_money(clean) or _unread_amount(clean))


def _missing(city: str | None, budget: int | None, minutes_found: bool) -> list[str]:
    return [name for name, absent in (("city", city is None), ("budget", budget is None),
                                      ("available_time", not minutes_found)) if absent]


# ---------------------------------------------------------------------------------------------------
# Clarification policy
# ---------------------------------------------------------------------------------------------------


def _location_currency(city: str | None, matches: list[Location] | None) -> tuple[bool, str | None]:
    """(known, currency) for the city: from the geocoder match, else from the gazetteer."""
    if matches:
        loc = matches[0]
        return True, loc.currency or currency_for_country(loc.country_code)
    if matches is None:
        place = lookup_place(city)
        if place is not None:
            return True, currency_for_country(place.country_code)
    return False, None


def _close_city_matches(city: str, matches: list[Location] | None) -> list[str]:
    options = _dedupe([loc.name for loc in matches or []])
    gaz = _gazetteer()
    names = {alias: place.city for alias, places in gaz.by_name.items() for place in places[:1]}
    for alias in difflib.get_close_matches(city.lower(), list(names), n=5, cutoff=0.75):
        options.append(names[alias])
    return _dedupe(options)[:5]


def clarification_questions(outcome: ParseOutcome, matches: list[Location] | None,
                            asked_before: bool) -> list[ClarificationQuestion]:
    """At most 2 blocking questions; [] when nothing blocks or asked_before is True (then defaults apply).
    Blocking: missing city, city not found (matches == []), ambiguous currency (non-INR country and no
    explicit currency), constraint conflicts, unmappable hard-sounding constraints.

    Priority: city / city_not_found > conflict > currency > budget_time > constraint. ``budget_time`` is
    asked only when the city is missing too (fully vague input); otherwise budget and time get defaults.
    Instruction-like unmapped text is never asked about."""
    if asked_before:
        return []
    prefs = outcome.preferences
    questions: list[ClarificationQuestion] = []
    city_missing = prefs.city is None or "city" in outcome.missing
    if city_missing:
        questions.append(ClarificationQuestion(
            id="city", question="Which city are you spending Saturday in?", options=list(CITY_OPTIONS),
            field="city"))
    elif matches is not None and not matches:
        city = prefs.city or ""
        close = _close_city_matches(city, matches)
        questions.append(ClarificationQuestion(
            id="city_not_found",
            question=f"I couldn't find a city called “{city}”. Which city did you mean?",
            options=close or list(CITY_OPTIONS), field="city"))
    if outcome.conflicts:
        a, b = _conflict_options(outcome.conflicts[0])
        questions.append(ClarificationQuestion(
            id="conflict", question=f"“{a}” and “{b}” can't both happen in one plan. Which should I keep?",
            options=[a, b], field="constraints"))
    if NEGATIVE_BUDGET_NOTE in prefs.assumptions:
        questions.append(ClarificationQuestion(
            id="budget", question="Your budget is negative. What non-negative budget should I use per person?",
            options=["0", "500", "1000"], field="budget"))
    if not city_missing and prefs.budget is not None and not outcome.budget_currency_explicit:
        known, local = _location_currency(prefs.city, matches)
        if known and local != "INR":
            options = [local, "INR"] if local else ["INR", "USD"]
            question = (f"Is your budget of {prefs.budget} in {local} or INR?" if local
                        else f"Which currency is your budget of {prefs.budget} in?")
            questions.append(ClarificationQuestion(id="currency", question=question, options=options,
                                                   field="currency"))
    if city_missing and {"budget", "available_time"} & set(outcome.missing):
        questions.append(ClarificationQuestion(
            id="budget_time", question="Roughly how much time do you have, and what's your budget per person?",
            options=list(BUDGET_TIME_OPTIONS), field=None))
    for text in outcome.unmapped_constraints:
        if not _is_instruction_like(text):
            questions.append(ClarificationQuestion(
                id="constraint",
                question=f"I can't check “{text}” against place data. Can you rephrase it, or keep it as a "
                         f"preference?",
                options=[KEEP_AS_PREFERENCE, SKIP_IT], field="constraints"))
            break
    return questions[:MAX_QUESTIONS]


# ---------------------------------------------------------------------------------------------------
# apply_answers
# ---------------------------------------------------------------------------------------------------

_DROP_ANSWERS = {"skip it", "skip", "drop it", "drop", "remove it", "remove", "ignore it", "ignore", "never mind",
                 "nevermind", "none", "forget it"}
_KEEP_ANSWERS = {KEEP_AS_PREFERENCE.lower(), "keep it", "keep", "preference", "as a preference",
                 "keep as a preference", "it's a preference", "just a preference"}
_NEITHER_ANSWERS = {"neither", "drop both", "remove both", "none of them"}


def apply_answers(request: PlanRequest, answers: dict[str, str]) -> PlanRequest:
    """Fold clarification answers (keyed by question id) back into the request.

    Keys: city, city_not_found, currency, conflict, budget_time, constraint (question ids) and the field
    names budget, available_time, start_time, neighbourhood, mood. Unknown keys and unparseable answers are
    ignored. Returns a new, validated PlanRequest; ``request`` is not modified."""
    data: dict[str, Any] = request.model_dump()
    for key, raw in answers.items():
        value = str(raw).strip() if raw is not None else ""
        if not value:
            continue
        if key in ("city", "city_not_found"):
            data["city"] = value[:80]
        elif key == "currency":
            code = _currency_from_answer(value, data.get("city"))
            if code:
                data["currency"] = code
        elif key == "conflict":
            _resolve_conflict(data, value)
        elif key == "budget_time":
            _apply_budget(data, value)
            _apply_time(data, value)
        elif key == "budget":
            _apply_budget(data, value)
        elif key in ("available_time", "time"):
            _apply_time(data, value)
        elif key == "start_time":
            start = _structured_start(value)
            if start is not None and start < 1440:
                data["start_time"] = _hhmm(start)
        elif key == "constraint":
            _replace_unmapped(data, value)
        elif key == "neighbourhood":
            data["neighbourhood"] = value[:80]
        elif key == "mood":
            data["mood"] = value[:300]
    return PlanRequest.model_validate(data)


def _apply_budget(data: dict[str, Any], value: str) -> None:
    money = _extract_money(_normalize_text(value), bare_ok=True)
    if money is None or not 0 <= money.amount <= _MAX_BUDGET:
        return
    data["budget"] = max(0, money.amount)
    if money.explicit and money.currency:
        data["currency"] = money.currency


def _apply_time(data: dict[str, Any], value: str) -> None:
    info = _extract_time(_normalize_text(value))
    if info.minutes is not None:
        data["available_time"] = info.minutes
    if info.start is not None and not info.start_assumed and info.start < 1440:
        data["start_time"] = _hhmm(info.start)


def _match_conflict_choice(answer: str, conflict: ConstraintConflict) -> str | None:
    low = answer.strip().lower()
    if low in _NEITHER_ANSWERS:
        return "neither"
    options = _conflict_options(conflict)
    for side, con, option in (("a", conflict.a, options[0]), ("b", conflict.b, options[1])):
        if low in {option.lower(), con.source_text.strip().lower(), constraint_label(con).lower()}:
            return side
    keys = {c.key for c in map_constraint_text(answer)}
    if conflict.a.key in keys and conflict.b.key not in keys:
        return "a"
    if conflict.b.key in keys and conflict.a.key not in keys:
        return "b"
    words = _content_words(answer)  # free-typed reply: pick the side sharing more words, if one clearly does
    overlap_a = len(words & (_content_words(options[0]) | _content_words(constraint_label(conflict.a))))
    overlap_b = len(words & (_content_words(options[1]) | _content_words(constraint_label(conflict.b))))
    if overlap_a != overlap_b:
        return "a" if overlap_a > overlap_b else "b"
    return None


_STOPWORDS = {"the", "a", "an", "and", "or", "please", "keep", "i", "want", "one", "must", "include",
              "only", "to", "of", "it", "that", "this", "with", "be", "is", "lets", "let's", "go"}


def _content_words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z']+", text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _resolve_conflict(data: dict[str, Any], answer: str) -> None:
    conflicts = detect_conflicts(original_constraints(PlanRequest.model_validate(data)))
    if not conflicts:
        return
    conflict = conflicts[0]
    choice = _match_conflict_choice(answer, conflict)
    if choice is None:
        return
    drop = {"a": [conflict.b], "b": [conflict.a], "neither": [conflict.a, conflict.b]}[choice]
    for con in drop:
        _remove_constraint(data, con.key)


def _strip_constraint_clauses(text: str, key: str) -> str:
    """``text`` without the clauses that map to ``key``; "" if the constraint still maps afterwards."""
    parts = [p.strip() for p in re.split(r",|;|\s+(?:and|but|then|also|plus)\s+", text, flags=re.IGNORECASE)]
    kept = [p for p in parts if p and key not in {c.key for c in map_constraint_text(p)}]
    rebuilt = ", ".join(kept)
    if key in {c.key for c in map_constraint_text(rebuilt)}:
        return ""
    return rebuilt


def _remove_constraint(data: dict[str, Any], key: str) -> None:
    new_constraints: list[str] = []
    for entry in data.get("constraints") or []:
        if key in {c.key for c in map_constraint_text(entry)}:
            kept = _strip_constraint_clauses(entry, key)
            if kept:
                new_constraints.append(kept)
        else:
            new_constraints.append(entry)
    data["constraints"] = new_constraints
    if data.get("free_text"):
        rebuilt: list[str] = []
        for sentence in _sentences(_normalize_text(data["free_text"])):
            if key in {c.key for c in map_constraint_text(sentence)}:
                sentence = _strip_constraint_clauses(sentence, key)
            if sentence:
                rebuilt.append(sentence)
        data["free_text"] = f"{'. '.join(rebuilt)}." if rebuilt else None


def _replace_unmapped(data: dict[str, Any], answer: str) -> None:
    unmapped, _ = _unmapped_and_soft(PlanRequest.model_validate(data))
    target = next((t for t in unmapped if not _is_instruction_like(t)), None)
    if target is None:
        return
    low = answer.strip().lower()
    if low in _KEEP_ANSWERS:
        return  # left as is; after this round it is disclosed as an unmapped preference
    drop = low in _DROP_ANSWERS
    replacement = answer.strip()[:120]
    constraints: list[str] = list(data.get("constraints") or [])
    if target in constraints:
        index = constraints.index(target)
        if drop:
            del constraints[index]
        else:
            constraints[index] = replacement
        data["constraints"] = constraints
        return
    free_text = _normalize_text(data.get("free_text"))
    if target in free_text:
        remaining = free_text.replace(target, "", 1)
        remaining = re.sub(r"\s*,\s*(?=[,.;!?]|$)|^\s*[,.;]\s*", "", remaining).strip()
        data["free_text"] = remaining or None
    if not drop:
        if len(constraints) < 10:
            constraints.append(replacement)
            data["constraints"] = constraints
        else:
            data["free_text"] = f"{data.get('free_text') or ''}. {replacement}".strip(". ")[:1000]


# ---------------------------------------------------------------------------------------------------
# apply_defaults
# ---------------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _fx() -> dict[str, Any]:
    path = DATA_DIR / "fx.yaml"
    if not path.exists():
        return {}
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data


def fx_as_of() -> str:
    """Date of the reference rates in data/fx.yaml (approximate, not live)."""
    return str(_fx().get("as_of", "undated"))


def convert_amount(amount: int, source: str, target: str) -> int | None:
    """Convert with the approximate reference rates in data/fx.yaml, rounded down so a converted budget never
    allows more than the original. None when either currency has no reference rate."""
    rates: dict[str, Any] = _fx().get("units_per_usd") or {}
    if source == target:
        return amount
    if source not in rates or target not in rates or not rates[source]:
        return None
    return math.floor(amount * float(rates[target]) / float(rates[source]) + 1e-9)


def apply_defaults(outcome: ParseOutcome, location: Location) -> Preferences:
    """Final preferences for planning: currency from the location when not explicit, a default budget per
    currency when missing (INR 1500, GBP 40, USD 50, EUR 45, else None), start 15:00 and 240 minutes when
    missing. Every default applied is appended to preferences.assumptions in plain words (no clarification)."""
    prefs = outcome.preferences.model_copy(deep=True)
    assumptions = list(prefs.assumptions)
    if prefs.city is None:
        prefs.city = location.name

    local = location.currency or currency_for_country(location.country_code)
    if not outcome.budget_currency_explicit or prefs.currency is None:
        if local and prefs.budget is not None:
            assumptions.append(f"Budget read as {_fmt_money(prefs.budget, local)} ({local}, the local currency)")
        prefs.currency = local  # a non-explicit guess is never kept; None when the country has no default
    elif local and prefs.currency != local:
        if prefs.budget is None:
            prefs.currency = local  # nothing to convert; the default budget below is in the local currency
        else:
            converted = convert_amount(prefs.budget, prefs.currency, local)
            if converted is not None:
                assumptions.append(
                    f"{_fmt_money(prefs.budget, prefs.currency)} ≈ {_fmt_money(converted, local)} at an approximate "
                    f"reference rate ({fx_as_of()}, not live)")
                prefs.budget, prefs.currency = converted, local
            else:  # left unconverted: the validator blocks plans priced in another currency
                assumptions.append(
                    f"Couldn't convert {_fmt_money(prefs.budget, prefs.currency)} to {local} (no reference rate), so "
                    f"places priced in {local} can't be checked against this budget")
    elif local is None and prefs.budget is not None:
        assumptions.append(f"No local currency is known for {location.name}; the budget stays in {prefs.currency}")

    if prefs.budget is None:
        default = DEFAULT_BUDGETS.get(prefs.currency or "")
        if default is not None:
            unread_values = [int(float(m.replace(",", ""))) for note in assumptions
                             if note.startswith(UNREAD_AMOUNT_PREFIX)
                             for m in re.findall(r"“([\d,]+(?:\.\d+)?)”", note)]
            prefs.budget = min([default, *unread_values])
            default = prefs.budget
            assumptions.append(f"Budget assumed to be {_fmt_money(default, prefs.currency)} per person")
        else:
            assumptions.append("No budget given, so costs are shown but not capped")
    elif prefs.group_size > 1:
        assumptions.append(
            f"Budget of {_fmt_money(prefs.budget, prefs.currency)} is per person (group of {prefs.group_size})")

    if "available_time" in outcome.missing:
        prefs.available_minutes = DEFAULT_MINUTES
        assumptions.append(f"Planning for {_fmt_span(DEFAULT_MINUTES)}")

    start_defaulted = "start_time" in outcome.missing or any(a.startswith(START_ASSUMPTION) for a in assumptions)
    if start_defaulted:
        assumptions = [a for a in assumptions if not a.startswith(START_ASSUMPTION)]
        prefs.start_min = LONG_DAY_START_MIN if prefs.available_minutes > LONG_WINDOW_MIN else DEFAULT_START_MIN
        assumptions.insert(0, _start_assumption(prefs.start_min))

    if not prefs.mood:
        assumptions.append("No mood given, so planning at a medium pace")
    if not prefs.interests:
        assumptions.append("No interests given, so the plan mixes food and easy sights")
    prefs.assumptions = _dedupe(assumptions)
    return prefs


# ---------------------------------------------------------------------------------------------------
# default_queries
# ---------------------------------------------------------------------------------------------------

# interest tag -> (intent base, category)
_INTEREST_QUERIES: dict[str, tuple[str, str]] = {
    "coffee": ("cafe", "cafe"), "relax": ("cafe", "cafe"), "books": ("bookstore", "bookstore"),
    "music": ("live music venue", "live_music_venue"), "live music": ("live music venue", "live_music_venue"),
    "nightlife": ("live music venue", "live_music_venue"), "food": ("restaurant", "restaurant"),
    "walks": ("park for a walk", "park"), "outdoors": ("park", "park"), "nature": ("garden or lake", "garden"),
    "art": ("art gallery", "art_gallery"), "museums": ("museum", "museum"), "culture": ("museum", "museum"),
    "history": ("heritage walk", "heritage_walk"), "shopping": ("market", "market"),
    "movies": ("cinema", "cinema"), "games": ("game arcade", "game_arcade"), "theatre": ("theatre", "theatre"),
    "dessert": ("dessert place", "dessert"), "street food": ("street food", "street_food"),
    "drinks": ("bar", "bar"), "wellness": ("spa", "spa"), "views": ("viewpoint", "viewpoint"),
}
_INCLUDE_QUERIES: dict[str, tuple[str, str]] = {
    "sit_down_meal": ("restaurant for a sit-down meal", "restaurant"),
    "outdoor_walk": ("park for an outdoor walk", "park"), "live_music": ("live music venue", "live_music_venue"),
    "coffee": ("cafe", "cafe"), "museum": ("museum", "museum"), "art": ("art gallery", "art_gallery"),
    "books": ("bookstore", "bookstore"), "park": ("park", "park"), "shopping": ("market", "market"),
    "dessert": ("dessert place", "dessert"),
}
_INDOOR_ALTERNATIVES: dict[str, tuple[str, str]] = {
    "park": ("art gallery", "art_gallery"), "garden": ("museum", "museum"), "lake": ("museum", "museum"),
    "heritage_walk": ("history museum", "museum"), "viewpoint": ("cafe", "cafe"),
    "market": ("bookstore", "bookstore"), "street_food": ("restaurant", "restaurant"),
}
_AVOID_CATEGORIES: dict[str, set[str]] = {
    "mall": {"mall"}, "bar": {"bar"}, "brewery": {"brewery"}, "market": {"market"}, "cinema": {"cinema"},
    "shopping": {"mall", "market"}, "alcohol": {"bar", "brewery"},
}
_QUIET_CATEGORIES = {"cafe", "restaurant"}
_PRICE_THRESHOLDS: dict[str, tuple[int, int]] = {"INR": (500, 1200), "GBP": (15, 35), "USD": (20, 45),
                                                 "EUR": (18, 40)}
_NEIGHBOURHOOD_RADIUS_M = 3000
_CITY_RADIUS_M = 8000
MIN_QUERIES = 3
MAX_QUERIES = 8


def _bias(prefs: Preferences, ctx: WorldContext) -> tuple[float, float, int]:
    loc = ctx.location
    if prefs.neighbourhood:
        hits = _gazetteer().by_name.get(prefs.neighbourhood.strip().lower(), [])
        for place in hits:
            if not place.is_city and _haversine_km(place.lat, place.lng, loc.lat, loc.lng) <= _CITY_RADIUS_KM:
                return place.lat, place.lng, _NEIGHBOURHOOD_RADIUS_M
    return loc.lat, loc.lng, _CITY_RADIUS_M


def _diet_word(constraints: list[Constraint]) -> str:
    values = {c.value for c in constraints if c.kind == "diet"}
    for value in ("vegan", "jain", "vegetarian"):
        if value in values:
            return value
    return ""


def _max_price_level(prefs: Preferences) -> int | None:
    if prefs.budget is None or not prefs.currency or prefs.currency not in _PRICE_THRESHOLDS:
        return None
    cheap, moderate = _PRICE_THRESHOLDS[prefs.currency]
    if prefs.budget <= cheap:
        return 1
    if prefs.budget <= moderate:
        return 2
    return None


def default_queries(prefs: Preferences, ctx: WorldContext) -> list[PlaceQuery]:
    """Place searches covering the user's interests, a meal if the window spans a meal time, hard
    constraints (e.g. "vegetarian restaurant"), and indoor options when rain is likely. 3-8 queries,
    biased to prefs.neighbourhood when known. Used by the no-LLM baseline and the scripted fake agent."""
    cats = load_categories()
    keys = {c.key for c in prefs.hard_constraints}
    diet = _diet_word(prefs.hard_constraints)
    quiet = "crowd:avoid" in keys or prefs.energy == Energy.LOW
    access = "access:wheelchair" in keys
    indoors_only = "setting:indoors_only" in keys
    outdoors_only = "setting:outdoors_only" in keys
    rainy = ctx.weather.is_rainy
    blocked: set[str] = set()
    for c in prefs.hard_constraints:
        if c.kind == "must_avoid":
            blocked |= _AVOID_CATEGORIES.get(c.value, {c.value})
    if "alcohol:avoid" in keys:
        blocked |= {k for k, info in cats.items() if info.alcohol}
    price_level = _max_price_level(prefs)
    lat, lng, radius = _bias(prefs, ctx)

    specs: list[tuple[str, str, int | None]] = []  # (intent, category, open_at_min)
    seen: set[str] = set()

    def add(base: str, cat: str, *, open_at: int | None = None, indoor: bool = False, key: str | None = None,
            ) -> None:
        dedupe_key = key or cat
        if dedupe_key in seen or cat in blocked or not is_category(cat):
            return
        info = cats[cat]
        food = info.group == "food"
        if (indoors_only and not info.indoor) or (outdoors_only and info.indoor and not food):
            return
        words = []
        if access:
            words.append("wheelchair accessible")
        if indoor:
            words.append("indoor")
        if quiet and cat in _QUIET_CATEGORIES:
            words.append("quiet")
        if food and diet:
            words.append(diet)
        words.append(base)
        seen.add(dedupe_key)
        specs.append((" ".join(words)[:200], cat, open_at))

    for c in prefs.hard_constraints:
        if c.kind == "must_include" and c.value in _INCLUDE_QUERIES:
            add(*_INCLUDE_QUERIES[c.value])
    if outdoors_only:
        for intent, cat in (("park", "park"), ("garden", "garden"), ("lake", "lake")):
            add(intent, cat)
    if ctx.start_min >= 21 * 60:
        add("late night cafe", "cafe")
        add("late night dessert", "dessert")
    if prefs.budget is not None and prefs.budget <= 100:
        add("free park", "park")
        add("free art gallery", "art_gallery")
    if rainy and not outdoors_only:
        add("museum or art gallery", "museum", indoor=True)
    windows = meal_windows_between(ctx.start_min, ctx.end_min)
    for w in windows:
        if w.name in ("lunch", "dinner"):
            add(f"restaurant for {w.name}", "restaurant", open_at=max(ctx.start_min, w.start_min),
                key=f"meal:{w.name}")
        elif w.name == "breakfast":
            add("breakfast cafe", "cafe", open_at=max(ctx.start_min, w.start_min), key="meal:breakfast")
    for interest in prefs.interests:
        spec = _INTEREST_QUERIES.get(interest.lower())
        if spec is None:
            continue
        base, cat = spec
        if cat == "restaurant" and any(k.startswith("meal:") for k in seen):
            continue  # the meal query already covers food
        if rainy and not cats[cat].indoor and cat in _INDOOR_ALTERNATIVES:
            add(*_INDOOR_ALTERNATIVES[cat], indoor=True)
        add(base, cat)
    for w in windows:
        if w.name == "snack":
            add("cafe", "cafe", open_at=max(ctx.start_min, w.start_min))
    fillers = [("cafe", "cafe"), ("art gallery", "art_gallery") if rainy or indoors_only else ("park", "park"),
               ("museum", "museum"), ("restaurant", "restaurant"), ("bookstore", "bookstore"),
               ("park", "park")]
    if not prefs.interests:
        for base, cat in fillers[:3]:
            add(base, cat)
    for base, cat in fillers:
        if len(specs) >= MIN_QUERIES:
            break
        add(base, cat)

    queries: list[PlaceQuery] = []
    for intent, cat, open_at in specs[:MAX_QUERIES]:
        food = cats[cat].group == "food"
        queries.append(PlaceQuery(
            intent=intent, category=cat, near_lat=lat, near_lng=lng, radius_m=radius,
            max_price_level=price_level if food else None, open_at_min=open_at,
        ))
    return queries
