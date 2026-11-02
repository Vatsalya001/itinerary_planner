"""Command line: plan a Saturday from the terminal.

    uv run python -m app.cli plan --fixture                      # brief's example on the frozen world
    uv run python -m app.cli plan --free-text "Indiranagar, 1500 rupees, 4 to 8pm, vegan" --fixture
    uv run python -m app.cli plan --city Bangalore --budget 2000 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import Settings
from app.deps import Deps, build_deps, fixture_settings
from app.domain.categories import currency_symbol
from app.domain.schemas import PlanRequest, PlanResult
from app.domain.timeutil import fmt_clock

BRIEF_EXAMPLE = {
    "city": "Bangalore",
    "budget": 2000,
    "available_time": "4 hours",
    "mood": "tired but wants to do something fun",
    "interests": ["food", "music", "walks"],
    "constraints": ["vegetarian", "avoid crowded places"],
}


def _request_from_args(args: argparse.Namespace) -> PlanRequest:
    if args.free_text and not any([args.city, args.budget, args.time, args.mood, args.interest, args.constraint]):
        return PlanRequest(free_text=args.free_text, start_time=args.start)
    data: dict = dict(BRIEF_EXAMPLE) if not args.no_defaults else {}
    for key, value in {
        "city": args.city, "budget": args.budget, "available_time": args.time, "mood": args.mood,
        "start_time": args.start, "free_text": args.free_text,
    }.items():
        if value is not None:
            data[key] = value
    if args.interest:
        data["interests"] = args.interest
    if args.constraint:
        data["constraints"] = args.constraint
    return PlanRequest(**data)


def render(result: PlanResult) -> str:
    lines = [f"status: {result.status}  mode: {result.mode}  model: {result.model}  ({result.latency_ms} ms)"]
    if result.message:
        lines.append(result.message)
    for q in result.questions:
        lines.append(f"? {q.question}  options: {', '.join(q.options)}")
    plan = result.plan
    if plan:
        sym = currency_symbol(plan.totals.currency)
        lines.append(f"\n{plan.city} · {plan.date:%A %d %B %Y} · {fmt_clock(plan.start_min)}–{fmt_clock(plan.end_min)}"
                     f" · {plan.weather.summary or plan.weather.status}")
        for i, stop in enumerate(plan.stops):
            c = stop.candidate
            lines.append(f"  {fmt_clock(stop.start_min)}–{fmt_clock(stop.end_min)}  {c.name} [{c.category}]"
                         f"  {sym}{c.cost.min}–{sym}{c.cost.max} ({c.cost.basis})  source={c.source.provider}")
            lines.append(f"      why: {stop.why}")
            if stop.tradeoff:
                lines.append(f"      trade-off: {stop.tradeoff}")
            if i < len(plan.legs):
                leg = plan.legs[i]
                lines.append(f"      → {leg.mode} ~{leg.minutes} min, {sym}{leg.fare.min}–{sym}{leg.fare.max} (estimate)")
        t = plan.totals
        lines.append(f"  worst case {sym}{t.worst_case} of {sym}{t.budget} · {t.elapsed_min} of {t.available_minutes} min")
    for check in result.checks:
        lines.append(f"  [{check.status}] {check.name}: {check.detail}")
    if result.infeasible:
        lines.append(f"  infeasible: {result.infeasible.reason}")
        for r in result.infeasible.relaxations:
            lines.append(f"    - {r.label} {r.patch}")
    for d in result.degradations:
        lines.append(f"  degraded: {d}")
    for a in result.assumptions:
        lines.append(f"  assumption: {a}")
    return "\n".join(lines)


async def _plan(args: argparse.Namespace) -> int:
    settings = fixture_settings(fixture_weather=args.weather) if args.fixture else Settings()
    deps: Deps = build_deps(settings)
    request = _request_from_args(args)
    from app.baseline import run_baseline

    try:
        result = await run_baseline(request, deps)
    finally:
        if deps.http is not None:
            await deps.http.aclose()
    print(json.dumps(result.model_dump(mode="json"), indent=2) if args.json else render(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="saturday-planner")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="plan a Saturday")
    p.add_argument("--free-text")
    p.add_argument("--city")
    p.add_argument("--budget", type=float)
    p.add_argument("--time", help='available time, e.g. "4 hours"')
    p.add_argument("--start", help='start time, e.g. "15:00"')
    p.add_argument("--mood")
    p.add_argument("--interest", action="append")
    p.add_argument("--constraint", action="append")
    p.add_argument("--no-defaults", action="store_true", help="don't prefill the brief's example")
    p.add_argument("--fixture", action="store_true", help="use the frozen-world fixtures (offline)")
    p.add_argument("--weather", default="clear", help="fixture weather: clear | rain_85pct | unavailable")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "plan":
        return asyncio.run(_plan(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
