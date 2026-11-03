"""Use the user's window: stretch stop durations of a valid draft, and say honestly when time is left free.

OWNER: domain agent (Phase 1/2 follow-up). Pure functions; no I/O.

``fill_window`` never reorders or adds stops and never moves events. Durations grow in two stages: first up to
each candidate's default duration, then up to default + one third of the way to the maximum (low energy) or up
to the maximum (otherwise). Extra minutes go proportionally to the room each stop has (low energy: seated stops
first). Every candidate draft is re-checked with ``validate_plan``, so opening hours, event times, sunset for
outdoor walks, crowd windows, pacing walking minutes and the time window all still hold; costs do not depend on
duration, so the budget is unchanged. The original draft is returned when no stretch is valid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.domain.evidence import is_full_meal, meal_slot
from app.domain.schemas import (
    Candidate,
    CanonicalPlan,
    Constraint,
    Energy,
    PlanDraft,
    Preferences,
    WorldContext,
)
from app.domain.timeutil import fmt_clock, fmt_duration
from app.domain.validator import UNDERFILL_RATIO, duration_bounds, validate_plan
from app.providers.base import TravelEstimator

DEFAULT_TARGET_RATIO = 0.85


def _stage_caps(c: Candidate, energy: Energy) -> tuple[int, int]:
    """(stage 1 cap, stage 2 cap) for one stop: the default, then default + a third (low energy) or the max."""
    lo, hi = duration_bounds(c)
    default = min(max(c.duration_range[1], lo), hi)
    second = default + (hi - default) // 3 if energy == Energy.LOW else hi
    return default, max(default, second)


def _allocate(rooms: Sequence[int], need: int, groups: Sequence[Sequence[int]]) -> list[int]:
    """Split ``need`` minutes over stops by room, group by group (proportional inside a group)."""
    alloc = [0] * len(rooms)
    for group in groups:
        group_room = sum(rooms[i] for i in group)
        if need <= 0 or group_room <= 0:
            continue
        take = min(need, group_room)
        for i in group:
            alloc[i] = rooms[i] * take // group_room
        remainder = take - sum(alloc[i] for i in group)
        for i in sorted(group, key=lambda k: (-(rooms[k] - alloc[k]), k)):
            if remainder <= 0:
                break
            extra = min(remainder, rooms[i] - alloc[i])
            alloc[i] += extra
            remainder -= extra
        need -= take
    return alloc


def _with_durations(draft: PlanDraft, durations: Sequence[int]) -> PlanDraft:
    stops = [s.model_copy(update={"duration_min": d}) for s, d in zip(draft.stops, durations, strict=True)]
    return draft.model_copy(update={"stops": stops})


def fill_window(draft: PlanDraft, ledger: Mapping[str, Candidate], ctx: WorldContext, prefs: Preferences,
                constraints: list[Constraint], travel: TravelEstimator,
                target_ratio: float = DEFAULT_TARGET_RATIO) -> PlanDraft:
    """Stretch durations toward ``target_ratio`` of the window; the best valid draft, or ``draft`` unchanged."""
    report = validate_plan(draft, ledger, ctx, prefs, constraints, travel)
    if not report.ok or report.plan is None:
        return draft
    target = int(target_ratio * min(prefs.available_minutes, ctx.end_min - ctx.start_min))
    best, best_elapsed = draft, report.plan.totals.elapsed_min
    if best_elapsed >= target:
        return draft
    cands = [ledger[s.candidate_id] for s in draft.stops]
    durations = [s.duration_min for s in draft.stops]
    movable = [i for i, c in enumerate(cands) if c.kind != "event"]
    if not movable:
        return draft
    if prefs.energy == Energy.LOW:
        groups = [[i for i in movable if cands[i].seated], [i for i in movable if not cands[i].seated]]
    else:
        groups = [movable]

    def attempt(trial: Sequence[int]) -> tuple[PlanDraft, int] | None:
        candidate = _with_durations(draft, trial)
        checked = validate_plan(candidate, ledger, ctx, prefs, constraints, travel)
        if checked.ok and checked.plan is not None:
            for stop in checked.plan.stops:
                c = stop.candidate
                if is_full_meal(c) and meal_slot(stop.start_min, stop.end_min) is None:
                    return None
                if c.kind == "place" and c.hours.status != "known" and c.category not in {
                    "bar", "brewery", "live_music_venue", "theatre", "cinema", "game_arcade"
                } and not 540 <= stop.start_min < stop.end_min <= 1320:
                    return None
            return candidate, checked.plan.totals.elapsed_min
        return None

    for stage in (0, 1):
        need = target - best_elapsed
        if need <= 0:
            break
        caps = [_stage_caps(c, prefs.energy)[stage] if i in movable else durations[i] for i, c in enumerate(cands)]
        rooms = [max(0, caps[i] - durations[i]) for i in range(len(cands))]
        if not any(rooms):
            continue
        alloc = _allocate(rooms, need, groups)
        trial = [d + a for d, a in zip(durations, alloc, strict=True)]
        result = attempt(trial)
        if result is not None:
            durations, (best, best_elapsed) = trial, result
            continue
        # the even split broke a rule (closing time, sunset, walking cap...): grow one stop at a time instead
        for group in groups:
            for i in sorted(group, key=lambda k: (-rooms[k], k)):
                room = min(caps[i] - durations[i], target - best_elapsed)
                lo, hi = 0, room
                while lo < hi:  # largest extra that still validates
                    mid = (lo + hi + 1) // 2
                    probe = list(durations)
                    probe[i] += mid
                    if attempt(probe) is not None:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo > 0:
                    durations[i] += lo
                    checked = attempt(durations)
                    if checked is None:  # non-monotonic corner case: undo
                        durations[i] -= lo
                        continue
                    best, best_elapsed = checked
    return best


def free_time_note(plan: CanonicalPlan, ctx: WorldContext) -> str | None:
    """Code-generated (not model prose, so numbers are fine) note when the plan still uses under 60% of the
    window: how much time is left and from when. None otherwise."""
    available = plan.totals.available_minutes
    free = available - plan.totals.elapsed_min
    if available <= 0 or free <= 0 or plan.totals.elapsed_min >= UNDERFILL_RATIO * available:
        return None
    return (f"This plan leaves about {fmt_duration(free)} free after {fmt_clock(plan.end_min)} — time to rest or "
            "wander nearby.")
