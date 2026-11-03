# Requested-window policy — 3 November 2026

## Behaviour

- Target **85% of the requested window**, with **80–100%** accepted as the practical range. Total scheduled elapsed time includes visits, conservative transfers, buffers and necessary event waiting. The hard end time is never relaxed. For four hours, the target is 204 minutes and the lower margin is 192 minutes; the maximum is 240 minutes.
- Accepted agent proposals are filled within realistic visit limits, then proposals below 85% can use bounded search over already-returned candidates for a fuller validated option. Deterministic planning also targets 85%, and final ranking prefers an option at or above 80% when present among its finalists. This is bounded heuristic search, not proof of an optimal or feasible full-window plan.
- A final **flexible venue** can be shortened by **at most 15 minutes** to fit the end time, preserving its minimum visit length. Fixed events are not clipped. The final draft must still pass budget, opening hours, dietary/access evidence, walking limits and all other validation. Transfers and buffers are retained. Agent adjustments are disclosed in assumptions and trace metadata. Deterministic extensions can make the same safe fit when appending a venue.
- Coverage below 80% is shown as a **Partial itinerary** for supported windows (30+ minutes). Existing warnings and free-time notes use the same lower margin. Missing time is not fabricated into activities or arbitrary idle blocks. If constraints, hours, pacing or returned data prevent sufficient coverage, an honestly labelled partial result is preferable to an unsafe/invented outing.
- **Medium energy is the default when no mood is supplied**, including merged AI parsing. The default UI description and mood field no longer prefill tiredness. Explicit tired/low-energy requests retain low pacing; explicit high-energy requests retain their interpretation.
- Previously saved plans are historical snapshots; re-run to obtain the new planning behaviour.

## Verification

- Backend: **1,881 passed**, 11 live/Ollama cases deselected.
- Frontend: **146 passed**; lint/typecheck/build passed.
- Chromium browser suite: **21 passed** with the production bundle and local fixture API.
- Ruff and mypy passed (75 application source files).
- Offline workflow/fault eval: **32/32**.
- Synthetic regressions cover the 85% fill target, exact 80% UI boundary, 1/10/15-minute final clipping with travel/buffers, rejecting larger overruns and sub-minimum visits, immutable fixed events, preserving the budget guard, deterministic last-venue fitting, medium default versus explicit tiredness, and stricter utilization warnings.
- Existing midnight calendar tests now compare the exported end time to the actual validated schedule through the requested 1 AM end, rather than assume an earlier hour. The XSS browser regression verifies the reconstructed Google route is safe and malicious supplied URLs are absent.
- No hosted inference was needed for these checks. Earlier live runs in the plan-quality report predate this policy and are not evidence of its current coverage.

## Limits

The target does not override user constraints, event attendance requirements, venue evidence or realistic duration ranges. Budget or sparse live data can still make an 80%-filled outing impossible. Do not claim universal coverage, unlimited throughput or zero defects.
