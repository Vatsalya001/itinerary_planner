from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.domain.timeutil import (
    fmt_clock,
    fmt_duration,
    next_saturday,
    parse_clock,
    parse_duration,
    parse_time_range,
)

IST = "Asia/Kolkata"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 5, 10, 0, tzinfo=ZoneInfo(IST)), date(2026, 11, 7)),  # Thursday
        (datetime(2026, 11, 7, 9, 0, tzinfo=ZoneInfo(IST)), date(2026, 11, 7)),  # Saturday morning
        (datetime(2026, 11, 7, 21, 0, tzinfo=ZoneInfo(IST)), date(2026, 11, 14)),  # Saturday late
        (datetime(2026, 11, 8, 12, 0, tzinfo=ZoneInfo(IST)), date(2026, 11, 14)),  # Sunday
    ],
)
def test_next_saturday(now, expected):
    assert next_saturday(now, IST) == expected


def test_next_saturday_uses_city_timezone():
    # Friday 20:00 UTC is already Saturday 01:30 in India
    now = datetime(2026, 11, 6, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert next_saturday(now, IST) == date(2026, 11, 7)


@pytest.mark.parametrize(
    ("value", "minutes"),
    [
        (240, 240), ("4 hours", 240), ("4h", 240), ("1.5 hrs", 90), ("90 min", 90), ("90 minutes", 90),
        ("2 hours 30 minutes", 150), ("2h30", 150), ("half day", 240), ("evening", 240), ("three hours", 180),
        ("an hour", 60), ("240", 240), ("", None), (None, None), ("whenever", None), (0, None),
    ],
)
def test_parse_duration(value, minutes):
    assert parse_duration(value) == minutes


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("15:00", 900), ("3pm", 900), ("3:30 pm", 930), ("11 am", 660), ("noon", 720), ("12am", 0),
     ("12pm", 720), ("23:00", 1380), ("25:00", None), ("13pm", None), ("abc", None)],
)
def test_parse_clock(text, minutes):
    assert parse_clock(text) == minutes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I'm free 4 to 8pm today", (960, 1200)),
        ("from 11am to 3pm", (660, 900)),
        ("15:00-19:00", (900, 1140)),
        ("10 to 2pm", (600, 840)),
        ("10pm to 1am", (1320, 1500)),
        ("I have 4-8 friends", None),
    ],
)
def test_parse_time_range(text, expected):
    assert parse_time_range(text) == expected


def test_formatting():
    assert fmt_clock(900) == "3:00 PM"
    assert fmt_clock(0) == "12:00 AM"
    assert fmt_clock(1500) == "1:00 AM (+1)"
    assert fmt_duration(45) == "45 min"
    assert fmt_duration(90) == "1 h 30 min"
    assert fmt_duration(120) == "2 h"
