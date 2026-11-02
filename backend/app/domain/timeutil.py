"""Time helpers. Local times are minutes since local midnight on the target date (see schemas)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SATURDAY = 5
LATE_SATURDAY_CUTOFF_HOUR = 20  # after 20:00 on a Saturday, plan for the next one


def next_saturday(now: datetime, tz: str) -> date:
    """The coming Saturday in ``tz``. Today counts if it is Saturday before 20:00 local time."""
    local = now.astimezone(ZoneInfo(tz)) if now.tzinfo else now.replace(tzinfo=ZoneInfo(tz))
    days_ahead = (SATURDAY - local.weekday()) % 7
    if days_ahead == 0 and local.hour >= LATE_SATURDAY_CUTOFF_HOUR:
        days_ahead = 7
    return local.date() + timedelta(days=days_ahead)


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a": 1, "an": 1, "half an": 0.5,
}
_NAMED_SPANS = {
    "half day": 240, "half a day": 240, "full day": 600, "whole day": 600, "all day": 600,
    "afternoon": 240, "the afternoon": 240, "evening": 240, "the evening": 240, "morning": 180,
}


def _num(token: str) -> float | None:
    token = token.strip().lower()
    if token in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[token])
    try:
        return float(token)
    except ValueError:
        return None


def parse_duration(value: str | int | float | None) -> int | None:
    """Parse an available-time expression into minutes.

    Integers are minutes (API contract). Strings accept "4 hours", "4h", "1.5 hrs", "90 min",
    "2 hours 30 minutes", "2h30", "half day", "evening". Returns None when nothing parses.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value) if value > 0 else None
    text = value.strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    for phrase, minutes in sorted(_NAMED_SPANS.items(), key=lambda kv: -len(kv[0])):
        if re.fullmatch(rf"(about |around |roughly |the )?{phrase}", text):
            return minutes
    total = 0.0
    matched = False
    m = re.search(r"(\d+)\s*h(?:ours?|rs?)?\s*(\d+)\s*m?(?:in(?:ute)?s?)?\b", text)  # 2h30, 2 hours 30 min
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    for num, unit in re.findall(
        r"(\d+(?:\.\d+)?|half an|an|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
        r"\s*(hours?|hrs?|h|minutes?|mins?|m)\b",
        text,
    ):
        n = _num(num)
        if n is None:
            continue
        matched = True
        total += n * 60 if unit.startswith("h") else n
    if matched and total > 0:
        return round(total)
    return None


def parse_clock(text: str | None) -> int | None:
    """Parse a clock time into minutes since midnight: "15:00", "3pm", "3:30 pm", "noon", "11 am"."""
    if not text:
        return None
    t = re.sub(r"(\d)\.(\d{2})(?!\d)", r"\1:\2", text.strip().lower())  # "4.30pm" / "16.30" -> "4:30pm"
    t = t.replace(".", "")  # "p.m." -> "pm"
    if t in {"noon", "midday"}:
        return 12 * 60
    if t == "midnight":
        return 24 * 60
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 24:
        return None
    return hour * 60 + minute


def parse_time_range(text: str) -> tuple[int, int] | None:
    """Find a range like "4 to 8pm", "4-8 pm", "from 11am to 3pm", "15:00-19:00" inside ``text``.

    A bare start hour borrows the end's am/pm ("4 to 8pm" -> 16:00-20:00).
    """
    pattern = (
        r"(?:from\s+)?(\d{1,2}(?::\d{2})?)\s*(am|pm)?\s*(?:to|till|until|-|–)\s*"
        r"(\d{1,2}(?::\d{2})?)\s*(am|pm)?"
    )
    normalized = re.sub(r"(\d)\.(\d{2})(?!\d)", r"\1:\2", text.lower()).replace("a.m.", "am").replace("p.m.", "pm")
    for m in re.finditer(pattern, normalized):
        s_raw, s_ampm, e_raw, e_ampm = m.groups()
        if not (s_ampm or e_ampm or ":" in s_raw or ":" in e_raw):
            continue  # "4-8" alone is too ambiguous (could be a count)
        end = parse_clock(f"{e_raw}{e_ampm or ''}")
        start = parse_clock(f"{s_raw}{s_ampm or e_ampm or ''}")
        if start is None or end is None:
            continue
        if end <= start and not s_ampm and e_ampm == "pm":
            start = parse_clock(f"{s_raw}am")  # "10 to 2pm" means 10am-2pm
        if start is None:
            continue
        if end <= start and not s_ampm and not e_ampm and end < 12 * 60 and end + 12 * 60 > start:
            end += 12 * 60  # "11.30 to 1.30" means 11:30-13:30, not a 14-hour window
        if end <= start:
            end += 24 * 60
        return start, end
    return None


def fmt_clock(minutes: int) -> str:
    """Format minutes since midnight as "3:00 PM"; times after midnight get a "(+1)" suffix."""
    day, rem = divmod(int(minutes), 24 * 60)
    hour, minute = divmod(rem, 60)
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    out = f"{h12}:{minute:02d} {suffix}"
    return out + (" (+1)" if day >= 1 else "")


def fmt_duration(minutes: int) -> str:
    """Format minutes as "45 min", "1 h", "1 h 30 min"."""
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def local_datetime(day: date, minutes: int, tz: str) -> datetime:
    """Absolute datetime for ``minutes`` since local midnight of ``day`` in ``tz``."""
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz)) + timedelta(minutes=minutes)


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end
