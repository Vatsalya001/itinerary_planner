# Facility requirement verification — 4 November 2026

This change adds the bounded source-backed facilities described in [the feature contract](../VENUE_REQUIREMENTS.md). Ordinary request paths do not make facility-research HTTP calls. No model inference or paid provider requests were used for this verification.

## Completed checks

- Full offline backend suite: **1,954 passed**. Raw local run: `/tmp/saturday-verified-backend-tests.log` (not committed). Live/Ollama-marked tests remain excluded under the existing pytest settings.
- Ruff: `uv run ruff check app tests` passed.
- Mypy: `uv run mypy app` passed for **77 source files**.
- Frontend: `npm run lint`, **148 unit/component tests**, and `npm run build` passed.
- Browser: existing **21 Playwright tests** passed against the built UI and fixture FastAPI backend.
- Offline workflow evaluation: **32/32 hard checks passed** with the scripted fake agent; generated records remain under `/tmp/saturday-facility-offline-evals`.
- Added **73 backend regression cases** across domain, mocked provider and end-to-end graph tests. They cover source identity, quotes, questions/future/qualified claims, conflicts, stale evidence, no keys, quotas/timeouts/malformed/oversized responses, lookup limits/cache, Plan B, both planners, explicit keep/skip/rephrase and compound constraints preserving diet.
- Added two UI regressions for sourced confirmation and explicit unknown presentation.

## Scope and limits

These checks establish application behavior on controlled synthetic data. They do not certify venue facilities in the real world or guarantee support for arbitrary requirements. Eight facility types are researched conservatively; other mandatory requirements remain clarification/blocking cases. Source statements must still be checked for current availability directly with venues. The feature does not change the model, currency, weather, cost or scheduling policies.

The consolidated evaluation PDF and historical inference reports remain historical; this change does not relabel them as fresh model evaluations. New local verification outputs stay outside the repository. The private interview handbook remains uncommitted.
