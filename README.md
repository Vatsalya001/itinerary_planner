# Perfect Saturday Planner

Describe how you feel and what you have (city, budget, time, mood, interests, constraints) and get a timed plan for
the coming Saturday: real places and events, clock times, travel legs, a per-person worst-case cost, weather-aware
stops with an indoor Plan B, a reason for every stop, and a checks panel that says what was verified. A LangGraph
agent makes the choices; deterministic code does the arithmetic, validates every plan and takes over when the model
fails.

**Live URL:** to be added after deployment.

## Features

| Brief item (as framed in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §0-§1) | What is implemented | Where it lives |
|---|---|---|
| Plan from the brief's input (city, budget, available time, mood, interests, constraints) | The brief's JSON is accepted as-is by `POST /api/plan`; free text is accepted too and parsed by an LLM plus a rules parser | `backend/app/domain/schemas.py` (`PlanRequest`), `backend/app/domain/parser_rules.py`, `backend/app/graph/nodes/parse.py` |
| Itinerary with times and costs | Clock times on the coming Saturday, travel legs with fares, per-person cost ranges with their basis, worst case with a 10% contingency, time and money left | `backend/app/domain/schedule.py`, `travel.py`, `costs.py`; `frontend/src/components/PlanView.tsx` |
| Reason for every stop, trade-offs | Model-written `why` and `tradeoff` (numbers only from data), omissions with reasons | `backend/app/graph/prompts.py`, `backend/app/domain/validator.py` (`PROSE_NUMERIC_CLAIM`) |
| Constraints respected | Fixed vocabulary (diet, alcohol, wheelchair, crowds, indoor/outdoor, must include/avoid) enforced with positive evidence; unknown is never yes | `backend/app/domain/constraints.py`, `evidence.py`, `validator.py` |
| Bonus: real data | Google Places (with a key), curated OpenStreetMap snapshot for Bangalore, Open-Meteo weather and geocoding, Tavily + schema.org events | `backend/app/providers/` |
| Bonus: clarifying questions | Up to two blocking questions with quick replies through a LangGraph `interrupt`, resumed on answer | `backend/app/graph/nodes/clarify.py`, `frontend/src/components/ClarificationCard.tsx` |
| Weather awareness | Rain moves plans indoors and outdoor stops get an indoor Plan B; walks end before sunset | `backend/app/domain/fallback.py`, `validator.py`, `sun.py` |
| Agent with tools and a visible trace | Five read-only tools, a live SSE trace with each tool call's reason, durations and validation outcomes | `backend/app/graph/tools.py`, `backend/app/graph/runner.py`, `frontend/src/components/TracePanel.tsx` |
| Graceful failure | Model fallback chain, a deterministic planner, a salvaged checked draft, and an honest "no feasible plan" with one-tap relaxations | `backend/app/graph/nodes/fallback.py`, `backend/app/domain/infeasible.py`, `frontend/src/components/InfeasibleCard.tsx` |
| Sharing and export | Share links (30 days), Google Maps route, copy as text, WhatsApp, calendar (.ics), feedback | `backend/app/store/redact.py`, `frontend/src/lib/ics.ts`, `share.ts`, `PlanActions.tsx` |
| Multi-model evaluation | Eval harness with independent checks, a generated report and an `/evals` leaderboard | `backend/evals/`, `docs/EVAL_REPORT.md`, `frontend/src/pages/EvalsLeaderboard.tsx` |
| Production operations | Rate limits and a daily cap, security headers, JSON logs, Sentry and LangSmith gating, Docker, Render blueprint, CI | `backend/app/api/`, `backend/app/observability.py`, `Dockerfile`, `render.yaml`, `.github/workflows/ci.yml` |

## Screenshots

_Screenshots to be added: planner with the live trace, a finished plan, a clarification, a "no feasible plan" card,
and the `/evals` page._

## Quick start (macOS)

Prerequisites: [uv](https://docs.astral.sh/uv/) (`brew install uv`), Node.js 22+ (`brew install node`), and
optionally [Ollama](https://ollama.com/download) for local models.

```bash
cp .env.example backend/.env   # optional; edit keys and PRIMARY_MODEL here
make setup                     # uv sync (backend) and npm ci (frontend)
make ollama                    # optional: start Ollama, pull qwen3.5:9b, gemma4:e4b, granite4:tiny-h, smoke-test tool calls
make dev                       # API on http://127.0.0.1:8000, UI on http://127.0.0.1:5173 (proxies /api)
make serve                     # or: build the UI and serve UI + API from one process on http://127.0.0.1:8000
```

What runs with no keys at all: the default `PRIMARY_MODEL=ollama:qwen3.5:9b` (if Ollama is not running, every run
falls back to the rule-based planner and says so), places from the curated Bangalore OpenStreetMap snapshot,
weather and geocoding from Open-Meteo. Other cities need `GOOGLE_MAPS_API_KEY`.

Choosing a model (in `backend/.env`; IDs come from `backend/app/llm/models.yaml`):

| Model | Settings |
|---|---|
| Claude | `ANTHROPIC_API_KEY=...` and `PRIMARY_MODEL=anthropic:claude-opus-5-5` |
| Gemini or Grok | The registry entries `google_genai:<pending>` and `xai:<pending>` are placeholders. Pin a real model ID in `models.yaml` (replace the ID and remove `pending: true`), then set `GOOGLE_API_KEY` or `XAI_API_KEY` and `PRIMARY_MODEL=<that ID>` |
| A second provider as fallback | `FALLBACK_MODEL=<registry ID>` |
| Local model | `PRIMARY_MODEL=ollama:granite4:tiny-h` (or any pulled `ollama:<tag>`) |
| No LLM | `PRIMARY_MODEL=baseline:deterministic` |

Live data: `GOOGLE_MAPS_API_KEY` (restrict it to Places API (New)) and `TAVILY_API_KEY` switch `PLACES_PROVIDER=auto`
and `EVENTS_PROVIDER=auto` to the live providers. For a fully offline frozen world, set `PLACES_PROVIDER=fixture`,
`EVENTS_PROVIDER=fixture`, `WEATHER_PROVIDER=fixture`, `GEOCODER=fixture` and `FIXED_NOW=2026-11-05T10:00:00+05:30`.

From the terminal (run in `backend/`):

```bash
uv run python -m app.cli plan --fixture                                  # the brief, no LLM, frozen world
uv run python -m app.cli plan --fixture --model ollama:granite4:tiny-h --deadline 240 --trace
uv run python -m app.cli plan --free-text "Indiranagar, 1500 rupees, 4 to 8pm, vegan" --fixture --json
```

## Architecture

A FastAPI app serves the React SPA and the API from one origin. Each plan is a LangGraph run streamed over SSE:

```mermaid
graph TD
    START([Start]) --> guard_input
    guard_input --> parse_preferences
    parse_preferences --> resolve_location
    resolve_location --> check_clarity
    check_clarity -.-> ask_clarification
    check_clarity -.-> build_context
    check_clarity -.-> finalize
    ask_clarification --> parse_preferences
    build_context -.-> plan_agent
    build_context -.-> fallback_planner
    plan_agent -.-> tools
    plan_agent -.-> plan_agent
    plan_agent -.-> fallback_planner
    tools -.-> plan_agent
    tools -.-> validate_plan
    tools -.-> fallback_planner
    validate_plan -.-> finalize
    validate_plan -.-> plan_agent
    validate_plan -.-> fallback_planner
    fallback_planner -.-> finalize
    fallback_planner -.-> explain_infeasible
    explain_infeasible --> finalize
    finalize --> END([End])
```

* The agent (`plan_agent`) has five read-only tools: `search_places`, `search_events`, `estimate_travel`,
  `check_plan` and `submit_plan`, with per-run caps, a 10-turn limit and a 60-second run deadline.
* `validate_plan` recomputes the schedule, costs and every check from the run's ledger of returned candidates; failed
  submissions come back to the model as tool errors (up to two repairs).
* `fallback_planner` is a deterministic beam-search planner over the same candidates and checks; it is also the
  no-LLM baseline.
* Data sources: Google Places API (New), a curated OpenStreetMap snapshot of 41 Bangalore places (ODbL), Open-Meteo
  (geocoding and forecast; free tier is non-commercial), Tavily search restricted to allow-listed event sites with
  schema.org JSON-LD parsed in code, and a synthetic frozen world for tests and evals. Provenance:
  [backend/data/SOURCES.md](backend/data/SOURCES.md).
* Storage: SQLite locally or Postgres in production for share links, the run log, rate limits, feedback and the
  eval summary; LangGraph checkpoints only for in-flight or paused runs.

Details: [docs/HLD.md](docs/HLD.md) (design) and [docs/LLD.md](docs/LLD.md) (module by module).

### Tests and evals

| Command | What it runs |
|---|---|
| `make test` | Backend pytest suite (offline: no network, keys or Ollama) and frontend Vitest |
| `make lint`, `make typecheck` | ruff and oxlint; mypy and tsc |
| `cd backend && uv run pytest -m live` | Live provider and Postgres tests (need `RUN_LIVE=1` with keys, or a Postgres `DATABASE_URL`) |
| `cd backend && RUN_OLLAMA=1 uv run pytest -m ollama tests/graph/test_ollama_e2e.py` | One end-to-end plan with a local model |
| `cd frontend && npx playwright install chromium && npm run e2e` | Browser tests against a locally started backend on the frozen world |
| `make eval-offline` | Offline eval suite (scripted fake model and injected faults; what CI runs) |
| `make eval MODELS=baseline:deterministic,ollama:granite4:tiny-h REPEATS=1` | Model evals on the frozen world; add `ARGS="--max-cost-usd 5"` (or `--max-minutes`) to cap a run and `--resume` to finish it. With `JUDGE_MODEL` set, an LLM judge also scores the plans (`--judge-model none` turns it off) |
| `make eval-all` | Baseline, the local models, and Claude when `ANTHROPIC_API_KEY` is set |
| `make eval-report` | Regenerate `docs/EVAL_REPORT.md` and `backend/evals/results/summary.json` |

## Eval results

[docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) is generated from the JSONL records in `backend/evals/results/` and
shows, per model, hard pass rate, status accuracy, agent success, first-submission validity, tool-protocol errors,
clarification precision and recall, parse accuracy, security, latency and cost, with dev and holdout cases reported
separately and every number shown with its `n`. Baseline, hosted-API and local-model rows are separate populations,
and all comparisons run on the synthetic frozen world. Cost is shown per plan and per run, and runs the harness
could not finish are counted as failures, not dropped. The same summary feeds the `/evals` page. Read the report for
the current numbers and the list of models that have not been run yet.

## Deployment (Render + Neon)

1. Create a [Neon](https://neon.com) Postgres project in AWS ap-southeast-1 (Singapore) and copy its connection
   string (`postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require`; the pooled host works).
2. In Render: New -> Blueprint -> select this repository. `render.yaml` defines one Docker web service in Singapore
   on an always-on plan, with health check `/api/health` and deploys only after the GitHub checks pass.
3. Fill the `sync: false` values: `DATABASE_URL` (Neon), `ANTHROPIC_API_KEY`, and optionally `GOOGLE_MAPS_API_KEY`,
   `TAVILY_API_KEY`, `FALLBACK_MODEL`, `SENTRY_DSN`, `LANGSMITH_API_KEY` (with `TRACING_ENABLED=true`).
4. On startup the app creates its tables and the LangGraph checkpoint tables. `/api/health` answers 503 when the
   database is unreachable, so Render's health check catches a broken `DATABASE_URL`; its `places` field shows a
   rejected Maps key (`auth_error`). The image includes `backend/evals/results/summary.json` for `/evals`;
   `uv run python -m evals.report --publish` (from `backend/`) with the Neon `DATABASE_URL` also stores the summary in
   the database, and `/api/evals/summary` serves whichever copy was generated more recently.
5. Roll back from the Render dashboard (Events -> earlier deploy -> Rollback).

Local container: `make docker-build && make docker-run`, or `docker compose up --build` (add `--profile postgres`
and `COMPOSE_DATABASE_URL=postgresql://planner:planner@postgres:5432/planner` for a local Postgres).

## Configuration

All settings are environment variables (or `backend/.env`); the full list with defaults is in
[docs/LLD.md §11](docs/LLD.md#11-configuration-backendappconfigpy) and commented in [.env.example](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `PRIMARY_MODEL` / `FALLBACK_MODEL` | `ollama:qwen3.5:9b` / none | Registry IDs of the model chain |
| `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY` | none | Model provider keys |
| `PLACES_PROVIDER` | `auto` | `google` with `GOOGLE_MAPS_API_KEY`, else `osm_snapshot`; or `fixture` |
| `EVENTS_PROVIDER` | `auto` | `tavily` with `TAVILY_API_KEY`, else `none`; or `fixture` |
| `DATABASE_URL` | `sqlite:///./data/app.db` | SQLite file or Postgres URL |
| `RUN_DEADLINE_S` | 60 | Run deadline in seconds |
| `RATE_LIMIT_PER_10MIN`, `RATE_LIMIT_PER_DAY`, `DAILY_PLAN_CAP` | 15, 60, 200 | Per-IP and global plan limits |
| `TRUST_PROXY_HEADERS` | false | `true` behind Render's proxy |
| `SENTRY_DSN`, `TRACING_ENABLED`, `LANGSMITH_API_KEY` | none, false, none | Error tracking and tracing |
| `JUDGE_MODEL` | none | Default judge for `make eval` (the app never reads it) |

## Limitations

* Live providers (Google Places, Tavily, Open-Meteo), Claude, Postgres/Neon and the Render deploy were not exercised
  by the verification runs, which had no keys; see the verification reports.
* Without a Google key, places come only from the Bangalore snapshot. Fares, price bands and FX rates are dated
  estimates (price bands and cab fares are hand-set, roughest outside INR, GBP, USD and EUR; only the Indian auto
  tariffs come from reports of notified fares); "likely busy" is a heuristic.
* Small local models often fall back to the rule-based planner; no judge model has scored plan quality in the
  committed results.
* Gemini and Grok are placeholders until model IDs are pinned.
* Resume locks and caches are per process; the deployment is one instance.
* More: [docs/HLD.md §15](docs/HLD.md#15-known-limitations-and-risks).

## Documentation

| Document | Content |
|---|---|
| [docs/HLD.md](docs/HLD.md) | High-level design: context, components, flows, graph, principles, models, data, security, reliability, deployment, evals, limitations, plan vs implementation |
| [docs/LLD.md](docs/LLD.md) | Low-level design: every module, contract, rule, endpoint, table, setting and test suite |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | The original build plan (v2) |
| [AGENTS.md](AGENTS.md) | Engineering rules for humans and coding agents |
| [eval_cases.json](eval_cases.json) | Behaviour spec for the eval harness |
| [docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) | Generated eval report |
| [docs/verification/PHASE1.md](docs/verification/PHASE1.md), [PHASE2.md](docs/verification/PHASE2.md) | Independent verification reports |
| [backend/data/SOURCES.md](backend/data/SOURCES.md) | Data provenance and licences |

## AI tools used

_Template: edit this section to describe your own process in your own words._

This repository was built with Claude Code orchestrating multiple agents. Every commit carries a
`Co-Authored-By: Claude Opus 5.5` trailer, [AGENTS.md](AGENTS.md) sets the ownership and testing rules the coding
agents worked under, and the reports in [docs/verification/](docs/verification/) come from independent verification
passes that encoded most defects as strict-xfail tests before they were fixed.

- Tools used: _[list the tools and models you used]_
- What they produced: _[e.g. which parts were drafted by agents]_
- What I did myself: _[your review, decisions, code you wrote or changed, how you checked behaviour]_
- How the result was checked: _[e.g. the offline suite, the eval runs in docs/EVAL_REPORT.md, manual runs]_
