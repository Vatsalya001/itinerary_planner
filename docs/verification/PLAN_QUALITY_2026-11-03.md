# Plan-quality follow-up — 3 November 2026

## Reported production failures

- Mumbai `r_84c147ba0a9e`: completed in agent mode but scheduled only 63/240 minutes at an expensive café and omitted museums. The arithmetic was consistent; accepting a poor partial plan was the defect.
- Delhi `r_203d4466656e`: exhausted the four-turn Cerebras cap and used deterministic recovery. A tour-service listing was treated as a historical attraction without evidence of an actual tour start time. The LLM parse also turned an optional café request into a hard include and dropped explicit food interests.
- Gurugram `r_d798779b642c`: arithmetic was consistent (204/240 minutes, INR 990 estimate). Pricing and wider accessibility were correctly unverified; the remaining issues were a lost food interest and pedestrian-time/export wording.

These saved public results were inspected without new inference calls. Singapore was explicitly excluded from this follow-up.

## Changes

1. **Bounded quality adjustment:** after ordinary validity and duration filling, a severely underfilled AI proposal can be replaced with a better validated option from the existing candidate ledger. Replacement must add a stop, add at least 30 scheduled minutes, preserve already-covered interests and satisfy the same constraints/budget/time checks. It does not add network/model calls, invent candidates, or merely pad one café. Its optional threaded search has a 0.75-second wait cap; failure preserves the valid original and records the error type. The bounded worker may finish after a timeout. Changes are disclosed in trace metadata and a visible assumption; the UI says “Planned with” the model to describe the hybrid workflow accurately.
2. **Balanced scoring:** short travel remains preferred, but its penalty no longer overwhelms missing interests in a severely underfilled window. This is ranking, not a hard travel limit. The search remains heuristic and bounded.
3. **Clear time display:** requested window and scheduled activities are both shown. A schedule using less than 60% of a window of at least 90 minutes receives a prominent Partial itinerary notice. Missing time remains free; it is not disguised as an activity. If no fuller validated option is found, the partial plan remains labelled.
4. **Cerebras repair reserve:** when candidates exist, the penultimate turn is submission-only on the tight Cerebras cap, leaving a turn available for validation repair. The four-turn cap and five-call/minute limiter are unchanged. This reduces avoidable exhaustion; it does not guarantee no fallback.
5. **Tour-listing guard:** place records identified as tour services are not treated as scheduled historical outings. A requested museum/art/heritage search category is no longer sufficient evidence when provider types do not map to an attraction. Actual dated tour events still use event/time validation. Historical shared plans are not rewritten.
6. **Preference preservation:** explicit “enjoy/like/try to include” interests survive an incomplete LLM parse. Optional includes are not promoted to mandatory activities; explicit mandatory requirements remain enforced. Duplicate “Prefer nearby stops” / “nearby stops” preferences are merged.
7. **Evidence and exports:** the wider-accessibility and unknown-price caveats remain. Wheelchair-facing summaries identify standard pedestrian estimates; copied text uses an outdoor-transfer description and labels the total as estimated. Food stops explain their food-break role when there is no other interest match.

## Local verification

- Full backend: **1,871 passed; 11 live/Ollama cases deselected**.
- Full frontend: **144 passed**.
- Chromium browser suite: **21 passed**; production build and real local fixture API.
- Ruff passed; mypy passed across **75 source files**; frontend lint/typecheck/build passed.
- Workflow/fault eval: **32/32**.
- Deterministic baseline: **26/26**.
- New positive and negative tests cover quality recovery and its disclosure, budget/diet preservation, no alternative/single-stop cases, tour operators versus real landmarks/events, optional versus mandatory includes, dropped food interests, duplicate preferences, requested-window display and wheelchair export wording.
- No protected shared schema, provider protocol, configuration or dependency files were changed. No hosted model calls were used for local tests/evals.

## Public acceptance

Fresh Mumbai, Delhi and Gurugram runs must be recorded after deployment. Singapore is out of scope for this follow-up. Check completion mode, trace, actual selected venue type, schedule coverage, source caveats and share pages separately; a green code check is not an independent physical venue audit.

## Remaining boundaries

No universal zero-flaw or fully verified travel guarantee is made. The app cannot manufacture missing admission prices, route accessibility, tour schedules or live venue availability. Better-plan search covers only returned candidates and can time out; one-stop outings can be legitimate when requested or when no fuller option is found. Provider throttling, deadlines and bounded repair can still require labelled fallback. Keep fresh plans distinct from historical saved snapshots.
