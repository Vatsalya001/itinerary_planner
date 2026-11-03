# Perfect Saturday Planner

An AI agent that turns a city, budget, available time, mood, interests, and constraints into a practical Saturday itinerary. FastAPI and LangGraph run the agent; React presents the plan and streams its actual execution trace.

**Source:** https://github.com/Vatsalya001/itinerary_planner  
**Public app:** deployment must be completed and its anonymous-access URL verified before submitting. A local URL or GitHub URL is not the required hosted UI.

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

Example model configuration:

```dotenv
PRIMARY_MODEL=cerebras:gpt-oss-120b
CEREBRAS_API_KEY=your_key_here
# Optional second provider:
FALLBACK_MODEL=groq:openai/gpt-oss-120b
GROQ_API_KEY=your_key_here
```

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
make eval MODELS=cerebras:gpt-oss-120b REPEATS=1 ARGS="--max-cost-usd 1"
cd frontend && npm run e2e
```

Latest completion-pass results:

| Check | Result |
|---|---|
| Backend tests | 1,804 passed; 11 live/Ollama cases deselected; no expected failures |
| Frontend tests | 137 passed; no expected failures |
| Ruff, mypy, frontend typecheck/lint/build | Passed |
| Chromium browser end to end | 21 passed; no expected failures |
| Offline fault and workflow evals | 32/32 |
| Deterministic model-suite evals | 26/26 |
| Live Cerebras smoke, frozen world | 2/3; assignment example completed in agent mode; free-text case hit rate limit and returned a validated fallback |

The live sample is small and partial. It measures model orchestration on fixed data, not real venue accuracy. No judge score or large-sample reliability claim is made. Failed and rate-limited runs remain in the report.

[Fresh generated eval report](docs/verification/RELEASE_EVAL_REPORT.md) · [Completion checks and changes](docs/verification/RELEASE_CHECKS.md). Older model results remain in [the historical report](docs/EVAL_REPORT.md); they describe an earlier working tree.

## Deploy and submit

The existing `Dockerfile` builds the UI and serves it from FastAPI. `render.yaml` configures Render plus a Postgres connection. Its `starter` service plan is paid; review that choice before creating a service. Configure the database and model secrets in Render, never in the repository.

A simpler review demo can use one Render Docker service, `DATABASE_URL=sqlite:////tmp/saturday/app.db`, `FRONTEND_DIST=/app/frontend/dist`, a hosted model and its key, and the Bangalore snapshot. SQLite on an ephemeral filesystem resets on a redeploy; this is suitable for a disposable demo, not durable production sharing. `/api/health` must return HTTP 200.

See [submission checklist](docs/SUBMISSION.md) for deployment settings and anonymous smoke checks. The assignment requires a public URL: do not submit until a fresh browser can generate a plan there.

## Limitations

- Place prices and travel times are estimates with contingency, not guaranteed quotes. Crowd checks are heuristics.
- Confirmed wheelchair entrance evidence does not establish every facility or the entire walking route's accessibility.
- An indoor alternative cannot fulfill a hard outdoor-walk requirement. When no compatible alternative exists, the plan explicitly warns about rain instead of promising one.
- The beam search is bounded and heuristic. An inability to find a plan is not a proof that no real-world itinerary exists.
- Model limiters and resume locks are per process. Keep the demo at one instance; additional instances require shared coordination.
- Persisting the planner across navigation uses browser memory only. Reloading the page clears the unsaved form and active plan; share links persist subject to the configured store and TTL.
- Live Google Places, Tavily, Postgres, Docker and a public deployment were not verified in the completion pass. Offline HTTP adapters, SQLite, the browser flow, and a small live model sample were checked.

## How AI tools were used

Claude Code was used for the original implementation and audit scaffolding. Codex reviewed the code, fixed the recorded regressions and additional validation gaps, and ran deterministic tests, browser checks, and a small live-provider evaluation. The repository includes the resulting checks and the live rate-limit failure rather than reporting only successful examples.
