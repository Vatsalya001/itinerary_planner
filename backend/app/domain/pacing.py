"""Energy level -> pacing profile, and mood text -> energy level (rule-based)."""

from __future__ import annotations

import re

from app.domain.schemas import Energy, MealWindow, PacingProfile

# Walking minutes = walk-mode leg minutes + full duration of walking-category stops (parks, lakes, walks).
PROFILES: dict[Energy, PacingProfile] = {
    Energy.LOW: PacingProfile(
        energy=Energy.LOW, max_stops=3, max_walk_minutes=45, buffer_minutes=15,
        max_long_legs=1, long_leg_minutes=15, min_seated_stops=1,
    ),
    Energy.MEDIUM: PacingProfile(
        energy=Energy.MEDIUM, max_stops=4, max_walk_minutes=75, buffer_minutes=10,
        max_long_legs=2, long_leg_minutes=15, min_seated_stops=0,
    ),
    Energy.HIGH: PacingProfile(
        energy=Energy.HIGH, max_stops=6, max_walk_minutes=150, buffer_minutes=10,
        max_long_legs=3, long_leg_minutes=15, min_seated_stops=0,
    ),
}

MEAL_WINDOWS: list[MealWindow] = [
    MealWindow(name="breakfast", start_min=7 * 60 + 30, end_min=10 * 60 + 30),
    MealWindow(name="lunch", start_min=12 * 60, end_min=14 * 60 + 30),
    MealWindow(name="snack", start_min=16 * 60, end_min=18 * 60),
    MealWindow(name="dinner", start_min=19 * 60 + 30, end_min=22 * 60),
]

_LOW = r"tired|exhausted|drained|sleepy|lazy|chill|relax|calm|slow|low[- ]energy|rest|quiet|easy|cosy|cozy|unwind|burnt? ?out|meh"
_HIGH = r"energetic|energy to burn|full of energy|pack (a lot|it) in|adventur|excited|hyped|active|pumped|lots to do|go all out"


def profile_for(energy: Energy) -> PacingProfile:
    return PROFILES[energy]


def energy_from_mood(mood: str | None) -> Energy:
    """Rule-based energy estimate. The LLM parser may override it; this is the fallback."""
    text = (mood or "").lower()
    if re.search(_HIGH, text):
        return Energy.HIGH
    if re.search(_LOW, text):
        return Energy.LOW
    return Energy.MEDIUM


def meal_windows_between(start_min: int, end_min: int) -> list[MealWindow]:
    return [w for w in MEAL_WINDOWS if w.start_min < end_min and start_min < w.end_min]
