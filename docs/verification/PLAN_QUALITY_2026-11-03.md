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
7. **Honest backup summaries:** a follow-up live run exposed a backup summary that attributed spare time to pace and omitted missing interests. The deterministic summary now names uncovered interests and says no fuller itinerary was selected, without inventing a cause. A synthetic regression covers this. Historical saved snapshots retain their original summary.
8. **Evidence and exports:** the wider-accessibility and unknown-price caveats remain. Wheelchair-facing summaries identify standard pedestrian estimates; copied text uses an outdoor-transfer description and labels the total as estimated. Food stops explain their food-break role when there is no other interest match.

## Local verification

- Full backend: **1,872 passed; 11 live/Ollama cases deselected**.
- Full frontend: **144 passed**.
- Chromium browser suite: **21 passed**; production build and real local fixture API.
- Ruff passed; mypy passed across **75 source files**; frontend lint/typecheck/build passed.
- Workflow/fault eval: **32/32**.
- Deterministic baseline: **26/26**.
- New positive and negative tests cover quality recovery and its disclosure, budget/diet preservation, no alternative/single-stop cases, tour operators versus real landmarks/events, optional versus mandatory includes, dropped food interests, duplicate preferences, requested-window display and wheelchair export wording.
- No protected shared schema, provider protocol, configuration or dependency files were changed. No hosted model calls were used for local tests/evals.

## Public acceptance

Code release `48a78a2` passed [all CI jobs](https://github.com/Vatsalya001/itinerary_planner/actions/runs/41679470319), including Docker smoke and Postgres. Render initially marked that commit live while public health and assets still served `ac90d61`. A manual redeploy restored agreement: public health reported `48a78a2` before the following requests. Anonymous share pages were inspected separately.

| City / run | Observed result | Coverage and limits |
|---|---|---|
| Mumbai `r_7ea0e55eff6e` | Agent, no degradation, 6,989 ms; 175/240 min, INR 1,929 estimated upper cost | Two stops instead of the reported 63-minute café-only result. Museum interest remained uncovered and was explicitly disclosed. A transfer could take 25 min; short-travel preference was only partly met and warned. [Saved plan](https://itinerary-planner-r54t.onrender.com/p/e5AHWkTEwJA). |
| Delhi `r_19f3cd88cf1e` | Agent, no degradation, 18,298 ms; 90/120 min, INR 660 estimated upper cost | One repair, two submissions within three agent turns. Food and coffee survived parsing; the café stayed optional. No unscheduled walking-tour business was selected. It produced a café-only outing and disclosed the missing history interest; this is not full-interest coverage. [Saved plan](https://itinerary-planner-r54t.onrender.com/p/aqc7jOVTgpE). |
| Gurugram `r_e0aee77953c4` | Labelled deterministic fallback after no AI submission, 13,475 ms; 110/240 min, INR 880 estimated upper cost | One restaurant, prominent Partial itinerary, requested versus scheduled window, entrance evidence linked, wider accessibility unverified. Art/museums were not covered. This run revealed the additional backup-summary wording correction above; its historical saved summary predates that correction. [Saved plan](https://itinerary-planner-r54t.onrender.com/p/Au5xaJpBDMc). |

Each run used four model calls. The requests were spaced, but Delhi still recorded limiter wait; capacity is model-call based, not user-prompt based. These are individual observations, not a reliability benchmark or independent venue audit. Singapore was not run.

An earlier exploratory Mumbai request added “Avoid venues that require accompanying children or membership”; the parser asked to clarify this unsupported compound constraint (`r_169202599404`, one inference call). The table uses the reported outing without that added requirement. It is not counted as a completed plan.

The final backup-summary correction is covered by the full local suite and subsequent source CI; no additional hosted inference is needed to verify deterministic wording. Saved shared plans are immutable snapshots, so re-run to obtain newly worded summaries.

## Remaining boundaries

No universal zero-flaw or fully verified travel guarantee is made. The app cannot manufacture missing admission prices, route accessibility, tour schedules or live venue availability. Better-plan search covers only returned candidates and can time out; one-stop outings can be legitimate when requested or when no fuller option is found. Provider throttling, deadlines and bounded repair can still require labelled fallback. Keep fresh plans distinct from historical saved snapshots.
