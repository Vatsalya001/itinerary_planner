"""Web events: Tavily search restricted to an allow-list, then schema.org Event JSON-LD parsed in code.

No LLM ever reads page text. Only structured JSON-LD fields become candidates; descriptions are never
copied, names are cleaned and truncated to 120 characters, and every event must fall on ``ctx.date`` in
the city's timezone and carry an https source URL. Fetching is polite: identified user agent, robots.txt
respected, 5 s timeout, 1 MB cap, at most three pages per search.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import logging
import math
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import httpx

from app.domain.categories import category
from app.domain.schemas import Candidate, CostRange, Hours, ProviderName, Source, WorldContext
from app.providers.base import ProviderError
from app.providers.normalize import (
    category_default_cost,
    clean_text,
    dedupe,
    haversine_m,
    is_https_url,
    location_currency,
    maps_search_url,
    maps_text_search_url,
)

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
USER_AGENT = "SaturdayPlanner/0.1"
_DISALLOW_ALL = ["User-agent: *", "Disallow: /"]
DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = ("district.in",)
MAX_PAGES = 3
PAGE_TIMEOUT_S = 5.0
SEARCH_TIMEOUT_S = 8.0
MAX_PAGE_BYTES = 1_000_000
ROBOTS_MAX_BYTES = 64_000
FETCH_DEADLINE_S = 8.0  # wall-clock cap per robots.txt or page fetch (slow-drip servers)
APPROX_LOCATION_TAG = "approx_location"
APPROX_ADDRESS_PREFIX = "Location approximate - check the venue"
NAME_MAX = 120
MAX_EVENTS_PER_PAGE = 50
DEFAULT_EVENT_MINUTES = 120
MAX_EVENT_MINUTES = 720
CITY_RADIUS_M = 40_000

EVENT_TYPES = {
    "Event", "BusinessEvent", "ChildrensEvent", "ComedyEvent", "DanceEvent", "EducationEvent",
    "ExhibitionEvent", "Festival", "FoodEvent", "Hackathon", "LiteraryEvent", "MusicEvent", "SaleEvent",
    "ScreeningEvent", "SocialEvent", "SportsEvent", "TheaterEvent", "VisualArtsEvent", "CourseInstance",
}
_TYPE_TAGS: dict[str, list[str]] = {
    "MusicEvent": ["music", "live music"],
    "ComedyEvent": ["comedy"],
    "TheaterEvent": ["theatre", "culture"],
    "DanceEvent": ["dance", "music"],
    "ExhibitionEvent": ["art", "culture"],
    "VisualArtsEvent": ["art", "culture"],
    "FoodEvent": ["food"],
    "LiteraryEvent": ["books"],
    "ChildrensEvent": ["family", "kids"],
    "Festival": ["festival", "culture"],
    "ScreeningEvent": ["movies"],
    "SportsEvent": ["sports"],
    "EducationEvent": ["workshop"],
}
_NAME_TAGS: list[tuple[str, list[str]]] = [
    (r"\b(music|concert|gig|jazz|acoustic|band|live|unplugged|orchestra|carnatic|hindustani|dj)\b", ["music"]),
    (r"\bopen ?mic\b", ["open mic", "music"]),
    (r"\b(comedy|stand-?up|improv)\b", ["comedy"]),
    (r"\b(art|exhibition|gallery|painting|photography)\b", ["art"]),
    (r"\b(book|poetry|reading|literature)\b", ["books"]),
    (r"\b(food|brunch|dinner|tasting|market)\b", ["food"]),
    (r"\b(workshop|class)\b", ["workshop"]),
    (r"\b(walk|trail|heritage)\b", ["walks"]),
]


# ---------------------------------------------------------------------------
# JSON-LD extraction (pure)
# ---------------------------------------------------------------------------


class _LdJsonCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._active = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        kind = (dict(attrs).get("type") or "").split(";")[0].strip().lower()
        if kind == "application/ld+json":
            self._active = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._active:
            self.blocks.append("".join(self._buf))
            self._active = False

    def handle_data(self, data: str) -> None:
        if self._active:
            self._buf.append(data)


def _ld_blocks(html: str) -> list[Any]:
    collector = _LdJsonCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # malformed HTML: keep whatever was collected
        log.debug("html parse error while collecting JSON-LD")
    out: list[Any] = []
    for block in collector.blocks:
        text = block.strip()
        text = re.sub(r"^\s*(<!--|<!\[CDATA\[)", "", text)
        text = re.sub(r"(-->|\]\]>)\s*$", "", text).strip()
        if not text:
            continue
        try:
            out.append(json.loads(text))
        except (json.JSONDecodeError, RecursionError):
            continue
    return out


def _types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    items = raw if isinstance(raw, list) else [raw]
    return {re.split(r"[/:#]", t)[-1] for t in items if isinstance(t, str)}


def iter_event_objects(data: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield schema.org Event dicts from parsed JSON-LD: handles lists, ``@graph``, ``ItemList`` /
    ``itemListElement`` / ``ListItem.item``, ``mainEntity`` and ``subEvent`` nesting (bounded depth)."""
    if depth > 8:
        return
    if isinstance(data, list):
        for item in data[:200]:
            yield from iter_event_objects(item, depth + 1)
        return
    if not isinstance(data, dict):
        return
    if _types(data) & EVENT_TYPES:
        yield data
    for key in ("@graph", "itemListElement", "item", "mainEntity", "subEvent", "event", "events"):
        if key in data:
            yield from iter_event_objects(data[key], depth + 1)


def _first_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str):
                return v
    return None


def _parse_dt(value: Any, tz: ZoneInfo) -> datetime | None:
    text = _first_str(value)
    if not text:
        return None
    text = text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{1,2}:\d{2}", text):
        return None  # date-only or free text: no start time we can schedule against
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _to_float(value: Any) -> float | None:
    """A plain number (JSON number or numeric string such as "12.97"); None for anything else."""
    if isinstance(value, bool):
        return None
    numeric = isinstance(value, int | float) or (
        isinstance(value, str) and re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", value) is not None)
    if not numeric:
        return None
    f = float(value)
    return f if math.isfinite(f) else None


class _PriceError(ValueError):
    """A listed price that is present but not plain numeric (e.g. "1e3", "from 500", "TBA")."""


class _OtherCurrency(ValueError):
    """A listed price in a different currency from the city's."""


_CURRENCY_MARKS = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "$": "USD", "usd": "USD", "us$": "USD",
                   "£": "GBP", "gbp": "GBP", "€": "EUR", "eur": "EUR"}
_AMOUNT = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MARK = r"(?:[A-Za-z]{3}|Rs\.?|US\$|[₹$£€])"
_PRICE_TEXT = re.compile(
    rf"^(?:(?P<m1>{_MARK})\s*)?(?P<a>{_AMOUNT})\s*(?:/-)?\s*(?P<m2>{_MARK})?"
    rf"(?:\s*(?:-|–|—|to)\s*(?:(?P<m3>{_MARK})\s*)?(?P<b>{_AMOUNT})\s*(?:/-)?\s*(?P<m4>{_MARK})?)?$",
    re.IGNORECASE,
)


def parse_price(value: Any, currency: str) -> tuple[float, float] | None:
    """Listed price -> (min, max) in ``currency``; None when absent.

    Accepts JSON numbers and plain numeric text with optional currency marks, thousands separators and
    ranges: 1500, "1,500", "INR 1500", "₹1,500 - ₹2,000", "Rs. 499/-", "Free". Raises _PriceError for anything
    else (scientific notation, negative numbers, "from 500", "TBA") and _OtherCurrency for another currency."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise _PriceError("boolean price")
    if isinstance(value, int | float):
        f = float(value)
        if not math.isfinite(f) or f < 0:
            raise _PriceError("bad number")
        return f, f
    if not isinstance(value, str):
        raise _PriceError("bad type")
    text = re.sub(r"\s+", " ", value).strip()
    if re.fullmatch(r"(?i)free(?: entry| admission)?", text):
        return 0.0, 0.0
    m = _PRICE_TEXT.match(text)
    if not m:
        raise _PriceError("not a plain price")
    for mark in (m["m1"], m["m2"], m["m3"], m["m4"]):
        if mark is None:
            continue
        code = _CURRENCY_MARKS.get(mark.lower(), mark.upper() if len(mark) == 3 and mark.isalpha() else None)
        if code is None:
            raise _PriceError("unknown currency mark")
        if code != currency:
            raise _OtherCurrency(code)
    lo = float(m["a"].replace(",", ""))
    hi = float(m["b"].replace(",", "")) if m["b"] else lo
    if hi < lo:
        raise _PriceError("inverted range")
    return lo, hi


def _offers_cost(offers: Any, currency: str) -> CostRange | None:
    """Ticket price from ``offers`` (Offer / AggregateOffer / list). Offers in another currency are skipped;
    any listed price that cannot be read plainly makes the cost unknown (category estimate), so a listed
    maximum is never underestimated."""
    items = offers if isinstance(offers, list) else [offers]
    lows: list[float] = []
    highs: list[float] = []
    for offer in items[:20]:
        if not isinstance(offer, dict):
            continue
        cur = _first_str(offer.get("priceCurrency"))
        if cur and cur.strip().upper() != currency:
            continue  # never mix currencies
        try:
            price = parse_price(offer.get("price"), currency)
            low = parse_price(offer.get("lowPrice"), currency)
            high = parse_price(offer.get("highPrice"), currency)
        except _OtherCurrency:
            continue
        except _PriceError:
            return None
        if price is None and high is None and low is not None:
            return None  # "from X" with no ceiling: the maximum is unknown
        for rng in (price, low):
            if rng is not None:
                lows.append(rng[0])
        for rng in (price, high, low):
            if rng is not None:
                highs.append(rng[1])
    if not lows:
        return None
    lo, hi = math.floor(min(lows)), math.ceil(max(highs))
    return CostRange(min=lo, max=max(lo, hi), currency=currency, basis="free" if hi == 0 else "ticket_price")


def _location(loc: Any) -> tuple[str | None, str | None, float | None, float | None, bool]:
    """(venue name, address, lat, lng, online)."""
    items = loc if isinstance(loc, list) else [loc]
    for item in items:
        if isinstance(item, str):
            return clean_text(item, 120), None, None, None, False
        if not isinstance(item, dict):
            continue
        if "VirtualLocation" in _types(item):
            continue
        name = _first_str(item.get("name"))
        address = item.get("address")
        addr_text: str | None = None
        if isinstance(address, str):
            addr_text = address
        elif isinstance(address, dict):
            parts = [address.get(k) for k in ("streetAddress", "addressLocality", "addressRegion")]
            addr_text = ", ".join(p for p in parts if isinstance(p, str) and p.strip()) or None
        geo = item.get("geo")
        lat = lng = None
        if isinstance(geo, dict):
            lat, lng = _to_float(geo.get("latitude")), _to_float(geo.get("longitude"))
            if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
                lat = lng = None
        return (
            clean_text(html_lib.unescape(name), 120) if name else None,
            clean_text(html_lib.unescape(addr_text), 200) if addr_text else None,
            lat,
            lng,
            False,
        )
    online = any(isinstance(i, dict) and "VirtualLocation" in _types(i) for i in items)
    return None, None, None, None, online


def _status_ok(obj: dict[str, Any]) -> bool:
    status = _first_str(obj.get("eventStatus")) or ""
    if any(s in status for s in ("EventCancelled", "EventPostponed", "EventMovedOnline")):
        return False
    mode = _first_str(obj.get("eventAttendanceMode")) or ""
    return "OnlineEventAttendanceMode" not in mode


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _site_name(url: str | None) -> str | None:
    if not url:
        return None
    host = _host(url)
    return host[4:] if host.startswith("www.") else host or None


def event_to_candidate(
    obj: dict[str, Any],
    page_url: str | None,
    ctx: WorldContext,
    *,
    provider: ProviderName = "web_event",
    id_prefix: str = "ev:",
    attribution: str | None = None,
    retrieved_at: datetime | None = None,
    require_same_day: bool = True,
    require_https: bool = True,
) -> Candidate | None:
    """One schema.org Event -> Candidate, or None when it fails a rule.

    Rules: a start time is required; with ``require_same_day`` it must fall on ``ctx.date`` in the city's
    timezone; with ``require_https`` the source URL (the event's own URL when it is https on the same site
    as the page, else the page URL) must be https. Online, cancelled and postponed events are dropped, as
    are events geocoded more than 40 km from the city centre. Descriptions are never read.

    Events without coordinates are placed at the city centre and clearly labelled: tag "approx_location",
    an address starting "Location approximate - check the venue", and a Maps link that searches the venue
    name instead of pinning the centre."""
    tz = ZoneInfo(ctx.location.timezone)
    raw_name = _first_str(obj.get("name"))
    if not raw_name:
        return None
    name = clean_text(html_lib.unescape(raw_name), NAME_MAX)
    if not name or not _status_ok(obj):
        return None
    start = _parse_dt(obj.get("startDate"), tz)
    if start is None:
        return None
    if require_same_day and start.date() != ctx.date:
        return None

    event_url = _first_str(obj.get("url"))
    source_url = page_url if is_https_url(page_url) else None
    if is_https_url(event_url) and (page_url is None or _host(event_url or "") == _host(page_url)):
        source_url = event_url
    if require_https and not source_url:
        return None

    venue, address, lat, lng, online = _location(obj.get("location"))
    if online:
        return None
    tags: list[str] = ["events"]
    approx = lat is None or lng is None
    if lat is None or lng is None:
        lat, lng = ctx.location.lat, ctx.location.lng
        tags.append(APPROX_LOCATION_TAG)
    elif haversine_m(lat, lng, ctx.location.lat, ctx.location.lng) > CITY_RADIUS_M:
        return None

    event_day: date = start.date()
    start_min = start.hour * 60 + start.minute
    end_min = start_min + DEFAULT_EVENT_MINUTES
    end = _parse_dt(obj.get("endDate"), tz)
    if end is not None:
        delta = int((end - start).total_seconds() // 60)
        if 0 < delta <= MAX_EVENT_MINUTES:
            end_min = start_min + delta
        else:
            tags.append("end_time_estimated")
    else:
        tags.append("end_time_estimated")
    length = end_min - start_min

    types = _types(obj)
    is_music = "MusicEvent" in types
    for t in sorted(types):
        tags.extend(_TYPE_TAGS.get(t, []))
    lowered = name.lower()
    for pattern, extra in _NAME_TAGS:
        if re.search(pattern, lowered):
            tags.extend(extra)
            if "music" in extra:
                is_music = True

    currency = location_currency(ctx.location)
    cost = _offers_cost(obj.get("offers"), currency) or category_default_cost("event", currency)
    info = category("event")
    key = f"{source_url or ''}|{name}|{start.isoformat()}"
    cid = id_prefix + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    full_address = ", ".join(p for p in (venue, address) if p) or None
    if approx:  # no coordinates: placed at the city centre, so say so wherever the address is shown
        full_address = APPROX_ADDRESS_PREFIX + (f": {full_address}" if full_address else "")

    return Candidate(
        id=cid,
        kind="event",
        name=name,
        category="event",
        city_id=ctx.location.id,
        lat=lat,
        lng=lng,
        address=full_address,
        cost=cost,
        hours=Hours(status="known", periods=[(start_min, end_min)]),
        indoor=None,
        seated=info.seated,
        effort=info.effort,
        serves_alcohol=None,
        live_music=True if is_music else None,
        popularity="medium",
        busy_windows=[],
        tags=dedupe(tags),
        duration_range=(min(60, length), length, length),
        event_start_min=start_min,
        event_end_min=end_min,
        event_date=event_day,
        maps_url=(maps_text_search_url(f"{venue or name}, {ctx.location.name}") if approx
                  else maps_search_url(venue or name, lat, lng)),
        source=Source(
            provider=provider,
            url=source_url,
            retrieved_at=retrieved_at,
            attribution=attribution or _site_name(source_url),
        ),
    )


def extract_events_from_html(html: str, url: str, ctx: WorldContext) -> list[Candidate]:
    """Pure: schema.org Event JSON-LD in ``html`` (fetched from ``url``) -> same-day event candidates."""
    now = datetime.now(UTC)
    out: list[Candidate] = []
    seen: set[str] = set()
    for block in _ld_blocks(html):
        for obj in iter_event_objects(block):
            try:
                cand = event_to_candidate(obj, url, ctx, retrieved_at=now)
            except Exception:  # one odd event object never sinks the page
                log.debug("skipped an unreadable JSON-LD event")
                continue
            if cand is not None and cand.id not in seen:
                seen.add(cand.id)
                out.append(cand)
            if len(out) >= MAX_EVENTS_PER_PAGE:
                return out
    return out


def filter_and_rank_events(events: list[Candidate], interests: list[str], max_price: int | None) -> list[Candidate]:
    """Drop events whose cheapest ticket is above ``max_price``; rank by interest overlap, then start time."""
    wanted = {i.strip().lower() for i in interests if i.strip()}
    kept = [e for e in events if max_price is None or e.cost.min <= max_price]

    def key(e: Candidate) -> tuple[int, int, str]:
        tags = {t.lower() for t in e.tags}
        overlap = sum(1 for w in wanted if w in tags or any(w in t for t in tags))
        return (-overlap, e.event_start_min or 0, e.id)

    return sorted(kept, key=key)


# ---------------------------------------------------------------------------
# Tavily provider
# ---------------------------------------------------------------------------


def _domain_allowed(url: str, allowed: tuple[str, ...]) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in allowed)


def _safe_interests(interests: list[str]) -> str:
    words = [re.sub(r"[^\w\s-]", " ", i)[:40].strip() for i in interests[:3]]
    return " ".join(w for w in words if w)


class TavilyEvents:
    """EventsProvider over Tavily search + JSON-LD parsing. ``name`` = "web_event"."""

    name = "web_event"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        allowed_domains: list[str] | tuple[str, ...] | None = None,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._allowed: tuple[str, ...] = tuple(d.lower().strip() for d in (allowed_domains or DEFAULT_ALLOWED_DOMAINS))
        self._max_pages = max(0, min(max_pages, MAX_PAGES))
        self._robots: dict[str, RobotFileParser] = {}

    def __repr__(self) -> str:  # never expose the key
        return f"TavilyEvents(allowed_domains={list(self._allowed)})"

    async def search(self, interests: list[str], ctx: WorldContext, max_price: int | None = None) -> list[Candidate]:
        when = f"{ctx.date.day} {ctx.date:%B %Y}"
        query = f"events in {ctx.location.name} on {when} {_safe_interests(interests)}".strip()
        urls = await self._search_urls(query)
        events: list[Candidate] = []
        seen: set[str] = set()
        for index, url in enumerate(urls[: self._max_pages]):
            html = await self._fetch_page(url)
            if html is None:
                continue
            try:
                found = extract_events_from_html(html, url, ctx)
            except Exception:  # an unreadable page is skipped, never a crash
                log.warning("tavily events: skipped unreadable page at index %d", index)
                continue
            for cand in found:
                if cand.id not in seen:
                    seen.add(cand.id)
                    events.append(cand)
        return filter_and_rank_events(events, interests, max_price)

    async def _search_urls(self, query: str) -> list[str]:
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": 5,
            "topic": "general",
            "include_domains": list(self._allowed),
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            resp = await self._client.post(
                TAVILY_URL,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                timeout=SEARCH_TIMEOUT_S,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("tavily", "timeout", "search timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("tavily", "network", type(exc).__name__, retryable=True) from exc
        if resp.status_code != 200:
            code = "rate_limited" if resp.status_code == 429 else f"http_{resp.status_code}"
            raise ProviderError("tavily", code, f"HTTP {resp.status_code}",
                                retryable=resp.status_code == 429 or resp.status_code >= 500)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("tavily", "bad_response", "invalid JSON") from exc
        results = data.get("results", []) if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise ProviderError("tavily", "bad_response", "unexpected response shape")
        urls: list[str] = []
        for result in results:
            url = result.get("url") if isinstance(result, dict) else None
            if isinstance(url, str) and is_https_url(url) and _domain_allowed(url, self._allowed) and url not in urls:
                urls.append(url)
        return urls

    async def _load_robots(self, origin: str) -> RobotFileParser:
        """robots.txt for ``origin``: streamed with a 64 KB cap and a wall-clock deadline. 401/403, 5xx,
        redirects, timeouts and network errors all mean "disallow"; other 4xx mean "no rules"."""
        parser = RobotFileParser()
        try:
            async with asyncio.timeout(FETCH_DEADLINE_S):
                async with self._client.stream(
                    "GET", origin + "/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=PAGE_TIMEOUT_S
                ) as resp:
                    status = resp.status_code
                    body = await _read_capped(resp, ROBOTS_MAX_BYTES) if status == 200 else b""
        except (httpx.HTTPError, TimeoutError):
            parser.parse(_DISALLOW_ALL)
            return parser
        if status == 200:
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
        elif status in (401, 403) or not 400 <= status < 500:
            parser.parse(_DISALLOW_ALL)
        else:
            parser.parse([])
        return parser

    async def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = await self._load_robots(origin)
            self._robots[origin] = parser
        return parser.can_fetch(USER_AGENT, url)

    async def _fetch_page(self, url: str) -> str | None:
        """Fetch one allow-listed https page politely; None on any failure (never raises)."""
        try:
            if not await self._robots_allows(url):
                log.info("robots.txt disallows %s", _site_name(url))
                return None
            async with asyncio.timeout(FETCH_DEADLINE_S):
                async with self._client.stream(
                    "GET", url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}, timeout=PAGE_TIMEOUT_S
                ) as resp:
                    if resp.status_code != 200:
                        return None
                    ctype = resp.headers.get("content-type", "")
                    if "html" not in ctype.lower():
                        return None
                    if not is_https_url(str(resp.url)) or not _domain_allowed(str(resp.url), self._allowed):
                        return None  # redirected off the allow-list
                    raw = await _read_capped(resp, MAX_PAGE_BYTES)
                    return raw.decode(resp.encoding or "utf-8", errors="replace")
        except (httpx.HTTPError, TimeoutError, UnicodeDecodeError, LookupError):
            return None


async def _read_capped(resp: httpx.Response, cap: int) -> bytes:
    """Read at most ``cap`` bytes of a streamed response body."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= cap:
            break
    return b"".join(chunks)[:cap]
