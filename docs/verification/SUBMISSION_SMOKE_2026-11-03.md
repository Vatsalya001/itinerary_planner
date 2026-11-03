# Public submission smoke — 3 November 2026

## Scope and environment

The public app at https://itinerary-planner-r54t.onrender.com/ was tested through the browser in a separate in-app browser session without an application login. This was not a Chrome incognito test. Health reported release `501a4914b9b266189547f913fbd002f661b2e3ff`, `env=prod`, model `groq:openai/gpt-oss-120b`, Google Places, Tavily configured, database `ok` and, after the lookup, places `ok`. No keys, private logs or Google place payloads are included here.

## Mumbai test

Prompt:

> Plan this Saturday in Mumbai from 3 PM to 7 PM for one person. My total budget is ₹2,000, including transport between stops. I'm tired but want an enjoyable outing. I like vegetarian food, coffee, and museums. Keep travel short and explain why each stop fits.

- Run ID: `r_c95f729542aa`.
- Stored status: `fallback`; mode: `deterministic`; stored latency: 6,738 ms. This is one observation, not a latency benchmark.
- Parsing completed with no blocking clarification; city/time/budget/interests/vegetarian constraint appeared in the UI.
- Trace showed separate parsing, location, weather and place-search work; place search used three queries and returned three candidates.
- A later model turn returned HTTP 429. The trace displayed the provider failure and requested 12-second Retry-After, and recovered without the hidden SDK wait. It does not identify which exact rate-limit bucket was exhausted.
- The fallback returned one stop, INR 1,540 worst-case including contingency, within INR 2,000; 90 of 240 minutes used.
- The UI explicitly labelled the rule-based fallback and warned that 150 minutes remained unused. Museums were not covered. This is partial soft-interest coverage, not a complete multi-stop outing.
- Budget, opening-hour, diet and source checks passed; time carried an underfilled-window warning. Provider evidence and hours are not an independent real-world venue audit.

## Sharing

The generated `/p/pHRE6MnfKo4` page loaded in a separate browser tab without login and displayed the saved Mumbai plan, fallback label, cost, source badge and underfilled-window warning. Its public API also returned the stored plan. Shared records are subject to configured TTL and provider rehydration; this link is smoke evidence, not a permanent fixture.

## Release verification and remaining gap

Release CI passed backend lint/types/tests/offline evals, frontend checks, real Postgres tests and Docker HTTP smoke checks: https://github.com/Vatsalya001/itinerary_planner/actions/runs/41652859288. Prior release checks recorded 1,847 backend tests, 21 browser tests, 32/32 workflow evals and 26/26 deterministic cases; this documentation pass did not repeat those suites.

This smoke verifies public availability, real retrieval, validation, visible quota recovery and sharing. It does not verify successful AI completion on the current provider allocation. Before evaluator testing, resolve the Groq quota and run one fresh prompt to confirm agent completion. No paid account action was performed.
