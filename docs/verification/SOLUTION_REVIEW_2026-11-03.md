# Solution review — 3 November 2026

The reviewed development build passes the checks below after the corrections recorded here. No unresolved reproducible defect was found in the reviewed paths. This is bounded verification, not proof that the application is bug-free or production-ready.

## Scope

Reviewed preference extraction and clarification, money/time boundaries, authoritative scheduling and constraint validation, fallback and failure flows, trace streaming, request handling, frontend form/run state, sharing, URL safety, and the local launcher. Existing repository tests and offline evals were rerun. Hosted provider calls were not made. A local model run was unnecessary: the discovered defects reproduce in pure parsing code, and the agent workflow was exercised with the scripted fake model.

## Corrections

- Response guidance variants such as “If that won't fit”, “I need you to explain the trade-offs”, and polite requests to separate verified accessibility from facts to check no longer become unknown venue constraints. Full-clause matching preserves mixed/unknown safety requirements.
- Clock values such as `24:30` are rejected. Exact `24:00` remains valid as an end-of-day boundary.
- Midnight start/end phrases retain the intended boundaries: midnight–4 AM starts at 00:00, and 10 PM–midnight ends at 24:00.
- Signed negative budgets are not converted to positive amounts. They prompt for a non-negative replacement and remain bounded at zero until corrected. A model parse cannot raise this bound. Tests cover clarification resume, spaced signs, Unicode minus and oversized numbers.

Added 23 regression cases across parser/time tests; no existing assertions were relaxed and no tests were marked as expected failures.

## Verification

| Check | Result |
| --- | --- |
| Backend offline tests | 1,833 passed; 11 live/Ollama tests deselected |
| Frontend unit tests | 137 passed |
| Browser end-to-end tests (final code) | 21 passed |
| Backend Ruff | Passed |
| Backend mypy | Passed (71 source files) |
| Frontend typecheck and oxlint | Passed |
| Production frontend build | Passed as part of browser test setup |
| Offline workflow/fault evals (final code) | 32/32 |
| Deterministic model-suite baseline (final code) | 26/26 |
| Diff whitespace check | Passed |

## Limits and remaining product work

- No live external-provider, quota-recovery or local-model quality assessment was performed in this review. Frozen-world eval success does not establish real-world venue accuracy or live-model performance.
- Venue prices, crowd levels and travel times can be estimates. Opening/accessibility evidence can be missing or stale; unknown evidence must remain unverified.
- Travel is estimated between itinerary stops. Travel from the user's initial location and the journey home are not costed or scheduled because no exact origin/return destination is supplied.
- Wheelchair checks establish venue evidence, not a verified step-free street/transit route or availability of an accessible vehicle.
- The assignment's public URL remains pending at the owner's request. Development mode is retained; no deployment or provider configuration was changed.
- `run.sh` and `docs/LOCAL_TESTING.md` remain separate local files from the earlier launcher request; this review does not include them in its source-fix commit.
