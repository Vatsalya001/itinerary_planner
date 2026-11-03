# Public submission smoke — 3 November 2026

## Scope and environment

The public app at https://itinerary-planner-r54t.onrender.com/ was tested through the browser in a separate in-app browser session without an application login. This was not a Chrome incognito test. Health reported release `501a4914b9b266189547f913fbd002f661b2e3ff`, `env=prod`, model `groq:openai/gpt-oss-120b`, Google Places, Tavily configured, database `ok` and, after the lookup, places `ok`. No keys, private logs or Google place payloads are included here.

## Earlier Mumbai test — Groq quota recovery

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

## Earlier sharing check

The generated `/p/pHRE6MnfKo4` page loaded in a separate browser tab without login and displayed the saved Mumbai plan, fallback label, cost, source badge and underfilled-window warning. Its public API also returned the stored plan. Shared records are subject to configured TTL and provider rehydration; this link is smoke evidence, not a permanent fixture.

## Release verification and remaining gap

Release CI passed backend lint/types/tests/offline evals, frontend checks, real Postgres tests and Docker HTTP smoke checks: https://github.com/Vatsalya001/itinerary_planner/actions/runs/41652859288. Prior release checks recorded 1,847 backend tests, 21 browser tests, 32/32 workflow evals and 26/26 deterministic cases; this documentation pass did not repeat those suites.

The earlier Groq smoke verified public availability, retrieval, validation, quota recovery and sharing. The later Cerebras smoke below verifies one successful live agent completion. No paid account action or billing-tier inspection was performed by the agent.

## Latest Mumbai acceptance — Cerebras

After the owner changed Render environment variables, health reported `version=1e46c83f529c145a31fc9368e7b61fcda68c32b9`, production mode and `primary_model=cerebras:gpt-oss-120b`. Database health was `ok`. This is an environment switch; the registry/Blueprint's Groq examples do not determine the live runtime.

The same Mumbai free-text prompt above was submitted in a fresh anonymous in-app browser tab (not a Chrome incognito window).

- Run: `r_73364567975f`; saved status `complete`, mode `agent`, model `cerebras:gpt-oss-120b`; no degradations.
- Stored latency: **15,025 ms**; one observation, not a performance guarantee.
- Trace: LLM parsing, city resolution, weather lookup, place search (three queries, 17 candidates), model submission, mandatory validation and finalization. The first submitted draft passed validation.
- Result: **two stops**, 15:00–18:35 on 7 November 2026, INR **1,205** worst-case including INR 110 contingency, within INR 2,000; **215/240 minutes** used.
- All eight displayed checks passed: budget, time, opening hours, diet, accessibility applicability, crowds heuristic, weather and sources. Passing a code check is not an independent real-world venue audit.
- The saved plan API was checked independently: correct agent status/model, costs within budget, elapsed time within the window and no degradation.
- Share page `/p/kEyAiCu8nXw` rendered both stops, cost, provider label and checks in a separate anonymous tab without login. Omitted unselected place names were redacted to generic placeholders on the shared page; this is the existing privacy behaviour. Links are subject to configured TTL and provider rehydration.

### Practical limits retained

The estimated transfer was 35–46 minutes (8.6 km), so “keep travel short” was only partly met. Transport modes/fares are city-level estimates, not route-specific availability or restriction checks; the suggested mode must be verified locally. Venue category does not prove age suitability, current admission price or live crowd conditions. The current Cerebras registry remains limited to five model calls per rolling minute and four agent turns; several back-to-back evaluator prompts can still wait or fall back. The account's paid/trial tier and credit balance were not inspected. One successful smoke does not establish sustained reliability.

The earlier quota-recovery failure remains recorded above; it is not relabelled as an AI success. No application source change was needed for this acceptance run.
