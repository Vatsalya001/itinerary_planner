# Perfect Saturday Planner

An AI agent that turns a city, budget, available time, mood, interests, and constraints into a practical Saturday itinerary. FastAPI and LangGraph run the agent; React presents the plan and streams its actual execution trace.

**Source:** https://github.com/Vatsalya001/itinerary_planner  
**Public app:** https://itinerary-planner-r54t.onrender.com/

**Submission verification:** [latest plan-quality fixes](docs/verification/PLAN_QUALITY_2026-11-03.md) · [practical guardrails and current checks](docs/verification/PRACTICAL_GUARDRAILS_2026-11-03.md) · [historical live smoke evidence](docs/verification/SUBMISSION_SMOKE_2026-11-03.md).

## What the app does

- Accepts the assignment JSON or a free-text description.
- Searches activities, food, and optional events through read-only tools.
- Shows named stops, clock times, estimated per-person costs, transfers, source badges, and why each stop fits.
- Validates budget, elapsed time, opening windows, dietary evidence, pacing, daylight, provenance, and rain alternatives in code.
- Asks up to two blocking clarification questions and resumes the paused run.
- Repairs invalid model proposals within a bounded attempt budget, then uses a deterministic fallback when necessary.
- Streams tool calls, durations, validation outcomes, and failures. The trace is execution metadata, not hidden model reasoning.

No booking, payment, shell, arbitrary URL-fetch, or external messaging tool is exposed to the agent.

## Architecture

```text
React form → FastAPI input boundary → parse and clarify → resolve city and weather
    → LangGraph agent ↔ search_places / search_events / estimate_travel / check_plan
    → submit_plan → deterministic validation → final result and SSE trace
                         ↓ invalid or provider failure
                 bounded repair → deterministic fallback → same validator
```

The server hydrates selected candidate IDs from the run's ledger and computes all costs and times. Model-written claims about amenities are replaced by explanations based on the supplied preferences and candidate evidence. This keeps an invented vegan menu, accessible toilet, or concert promise out of the displayed plan.

Search results can include a code-validated itinerary option with budget/time bounds and interest coverage, reducing the model’s arithmetic burden. Groq SDK retries are disabled so long provider retry delays recover visibly instead of silently consuming the run deadline. Full validation remains mandatory. See [performance release evidence](docs/verification/PERFORMANCE_RELEASE_2026-11-03.md).

The functions `parse_preferences`, `resolve_location`, `get_weather`, candidate searches, cost/schedule construction, and validation are separate implementations. `check_plan` and `estimate_travel` are optional agent tools; final validation is mandatory.

## Data and coverage

With `GOOGLE_MAPS_API_KEY`, the Places adapter searches supported cities. Without it, named places come from the bundled Bangalore OpenStreetMap snapshot. Other cities may return an honest no-data response. Open-Meteo provides geocoding and weather; Tavily event discovery is optional.

Synthetic fixtures are used for reproducible offline tests and carry synthetic provenance. A sourced snapshot is not live data. Prices, travel, crowd levels, and some venue properties are estimates; missing evidence is not treated as confirmation. See [data sources](backend/data/SOURCES.md).

## Run locally

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 22 or later.

```bash
cp .env.example backend/.env
make setup
# Edit backend/.env with your chosen provider settings, then:
make dev
```

Open `http://127.0.0.1:5173`. To serve the built UI and API together, run `make serve` and open `http://127.0.0.1:8000`.

Current public deployment model configuration (Render environment overrides the registry/Blueprint Groq example):

```dotenv
PRIMARY_MODEL=cerebras:gpt-oss-120b
CEREBRAS_API_KEY=your_key_here
FALLBACK_MODEL=
```

The live service now uses Cerebras GPT-OSS 120B. A Mumbai request completed in agent mode with no degradation; see the submission smoke report. Account billing/tier was not inspected. The Cerebras registry retains a trial-safe ceiling of 5 model calls per rolling minute and 4 agent turns, shared by requests in one process; allow at least a minute between demo runs. Provider quotas still apply. With no secondary model, failures use the validated deterministic planner. Groq remains an optional registered provider; the repository Blueprint still uses Groq unless its environment is changed.

Keep real secrets in `backend/.env` or the host's environment settings. Never commit them. Model availability and provider quotas depend on your account; a rate-limited model produces a labelled fallback.

For a deterministic demo with no LLM, set `PRIMARY_MODEL=baseline:deterministic`. For the entirely offline synthetic test world, use `PRIMARY_MODEL=fake:agent`, fixture providers, and the fixed clock shown in `.env.example`. These modes are labelled and are not evidence of live AI performance.

## Try the assignment input

```bash
curl http://127.0.0.1:8000/api/plan \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"city":"Bangalore","budget":2000,"available_time":"4 hours","mood":"tired but wants to do something fun","interests":["food","music","walks"],"constraints":["vegetarian","avoid crowded places"]}'
```

Use `Accept: text/event-stream` for the execution trace and final result. Also try an impossible budget, a vague request, and an unsupported city. Provider fault injection is test-only and ignored in production.

## Verification and evaluations

```bash
make test
make lint
make typecheck
make eval-offline
make eval MODELS=baseline:deterministic REPEATS=1
# With a real provider configured; expect API usage:
make eval MODELS=groq:openai/gpt-oss-120b REPEATS=1 ARGS="--max-cost-usd 1"
cd frontend && npm run e2e
```

Latest verified release results (see the [performance release evidence](docs/verification/PERFORMANCE_RELEASE_2026-11-03.md) and [CI run](https://github.com/Vatsalya001/itinerary_planner/actions/runs/41652859288)):

| Check | Result |
|---|---|
| Backend tests | 1,881 passed; 11 live/Ollama cases deselected; no expected failures |
| Frontend tests | 146 passed; no expected failures |
| Ruff, mypy, frontend typecheck/lint/build | Passed |
| Chromium browser end to end | 21 passed; no expected failures |
| Offline fault and workflow evals | 32/32 |
| Deterministic model-suite evals | 26/26 |
| Historical live Cerebras smoke, frozen world | 2/3; assignment example completed in agent mode; free-text case hit rate limit and returned a validated fallback |

An earlier Cerebras Mumbai run completed in agent mode and sharing worked, but subsequent review found admission and transport issues in that itinerary. Those checks proved orchestration, not full practical validity; the [practical guardrails release](docs/verification/PRACTICAL_GUARDRAILS_2026-11-03.md) addresses the recorded gaps. An earlier Groq run hit HTTP 429 and verified labelled fallback. These are individual smoke observations, not a success-rate or latency benchmark. The historical Cerebras sample is small and partial; it measures model orchestration on fixed data, not real venue accuracy. No judge score or large-sample reliability claim is made. Failed and rate-limited runs remain in the report.

[Earlier generated eval report](docs/verification/RELEASE_EVAL_REPORT.md) · [Completion checks and changes](docs/verification/RELEASE_CHECKS.md). Older model results remain in [the historical report](docs/EVAL_REPORT.md); they describe an earlier working tree.

## Deploy and submit

The existing `Dockerfile` builds the UI and serves it from FastAPI. `render.yaml` configures Render plus a Postgres connection. Its `starter` service plan is paid; review that choice before creating a service. Configure the database and model secrets in Render, never in the repository.

A simpler review demo can use one Render Docker service, `DATABASE_URL=sqlite:////tmp/saturday/app.db`, `FRONTEND_DIST=/app/frontend/dist`, a hosted model and its key, and the Bangalore snapshot. SQLite on an ephemeral filesystem resets on a redeploy; this is suitable for a disposable demo, not durable production sharing. `/api/health` must return HTTP 200.

See [submission checklist](docs/SUBMISSION.md) for deployment settings and anonymous smoke checks. The public UI and a saved Mumbai plan were checked without login. Use a fresh plan to apply the current admission/transport rules; old shared plans remain historical snapshots. Keep account credits/quota available for evaluators.

## Limitations

- Place prices and travel times are estimates with contingency, not guaranteed quotes. Crowd checks are heuristics.
- Confirmed wheelchair entrance evidence does not establish every facility or the entire walking route's accessibility.
- An indoor alternative cannot fulfill a hard outdoor-walk requirement. When no compatible alternative exists, the plan explicitly warns about rain instead of promising one.
- The beam search is bounded and heuristic. An inability to find a plan is not a proof that no real-world itinerary exists.
- Model limiters and resume locks are per process. Keep the demo at one instance; additional instances require shared coordination.
- Persisting the planner across navigation uses browser memory only. Reloading the page clears the unsaved form and active plan; share links persist subject to the configured store and TTL.
- The public Mumbai smoke exercised live Google Places, geocoding, weather, storage and anonymous sharing. Tavily is configured but event discovery was not exercised by this smoke. The [GitHub CI workflow](https://github.com/Vatsalya001/itinerary_planner/actions/workflows/ci.yml) checks real Postgres and builds/smoke-tests Docker. Offline checks do not establish live venue accuracy or provider capacity.
- The earlier Groq Mumbai run returned an underfilled fallback after HTTP 429; the later Cerebras run completed two stops. Its estimated transfer was 35–46 minutes, so the short-travel preference was only partly satisfied. Travel modes and fares are city-level heuristics, not route-specific availability checks; verify local transport restrictions and current admission/venue suitability. No broad live AI-completion rate or production latency benchmark is claimed.

## How AI tools were used

Claude Code was used for the original implementation and audit scaffolding. Codex reviewed the code, fixed the recorded regressions and additional validation gaps, and ran deterministic tests, browser checks, and a small live-provider evaluation. The repository includes the resulting checks and the live rate-limit failure rather than reporting only successful examples.

## Latest live acceptance

Release `89b963f` completed fresh Mumbai and London requests in Cerebras agent mode without degradation; both anonymous share pages worked. Mumbai returned a provisional INR 1,250 estimate with admission/long-transfer warnings. London returned a GBP 44 estimate, scoped entrance evidence, unverified wider accessibility and walking directions. See the practical guardrails report for exact runs, checks and limitations. These are smoke tests, not a reliability benchmark.

## Practical guardrail scope

Entrance accessibility is distinct from internal facilities and routes; the latter remain explicitly unverified. Museum admission estimates make budget checks provisional. Known child-accompaniment restrictions are enforced conservatively because ages are not represented. National Gallery general admission is distinguished from paid exhibitions. Mumbai motor travel uses cab estimates, and a short-travel preference can still require an explained compromise. See the current guardrails report for evidence and limitations; no zero-bug or comprehensive venue-verification claim is made.

## Requested time and plan quality

The UI separates the requested window from scheduled activity time and labels substantially underfilled plans as partial. A bounded local quality check can replace a short proposal with a fuller validated option from the returned places while preserving covered interests and hard constraints; that adjustment is disclosed. Optional activities remain optional, explicit interests are retained, and tour-business listings do not establish scheduled historical visits. See the latest plan-quality report for exact checks and limits.

Latest planning behaviour: [85% window target, safe final-venue fitting and medium default](docs/verification/WINDOW_POLICY_2026-11-03.md). Targets remain subordinate to venue evidence and hard constraints.

## Downloadable evaluation report

[Evaluation report (PDF)](output/pdf/EVALUATION_REPORT.pdf) consolidates the historical hosted/local-model comparisons, current code checks, methodology, provenance and open limitations. Historical scores are not presented as current-code model results.

To regenerate with ReportLab installed: `python docs/reports/build_evaluation_pdf.py`. The generator cross-checks historical hard-pass counts against their selected committed JSONL sessions. No inference calls are made.

### Browser-local itinerary history

Completed plans with a saved-plan link appear in **Your itinerary history**, labelled by city and date with the original request. The list shows up to 30 bookmarks from the last 30 days. Older entries are filtered out; the next history update rewrites the retained list. Open an entry to revisit its existing shared plan without another model/planning call; remove individual entries or clear history with confirmation. History is specific to this browser/device, and anyone using it can read the saved requests. Saved-plan links can expire or become unavailable after server storage resets.

Only city, date, request text, saved timestamp and share ID are kept in `localStorage`; venue payloads, traces, resume tokens and credentials are not copied there. Corrupt, disabled or full storage does not interrupt planning; unavailable storage uses a labelled in-tab fallback. The backend, model prompts, validator, budgets and scheduling logic remain unchanged from submitted commit `c272b7c`.
