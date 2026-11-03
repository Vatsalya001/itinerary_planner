# Perfect Saturday Planner: High-Level Design

Current implementation note from the completion pass: the model chooses candidate IDs, while displayed factual fit explanations are rendered from catalog evidence in `backend/app/domain/explanations.py`. Rain alternatives are validated as substituted routes, including transfer time and cost. See [release checks](verification/RELEASE_CHECKS.md) and [fresh evals](verification/RELEASE_EVAL_REPORT.md) for the current verified state; historical design targets below are not additional guarantees.

This document describes the system as implemented on `main` at `5643543`, plus this documentation. Where the code differs from [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md), the code is described and the difference is
listed in [Plan vs implementation](#16-plan-vs-implementation). Module-level detail is in [LLD.md](LLD.md).

## 1. Purpose and scope

The planner turns a free-text or form request ("Bangalore, ₹2000, 4 hours, tired but wants something fun, food,
music, walks, vegetarian, avoid crowds") into a timed Saturday itinerary: real places and events, clock times on the
coming Saturday, travel legs, a per-person cost range with a worst case, weather-aware choices with an indoor Plan B,
a reason for every stop, and a checks panel that says what was verified and what was not.

A LangGraph agent (LLM with read-only tools) makes the judgment calls: what to search for, which candidates to
combine, the order and length of stops, and the explanation. Deterministic code owns everything that must always be
true: input bounds, location and date, schedule and cost arithmetic, validation, the fallback planner and
persistence.

In scope (implemented): public API and single-page UI with no login; free text and form input (the brief's JSON is
accepted as-is); one clarification round through a LangGraph `interrupt`; live or frozen-world data providers; SSE
trace; share links; Google Maps route links; calendar export; rate limits and a daily plan cap; JSON logs, Sentry and
LangSmith gating; Docker image, Render blueprint and CI; an eval harness with a generated report and an `/evals` page.

Out of scope: accounts, bookings, payments, multi-day trips, and any tool with side effects. No tool can spend
money or contact anyone.

## 2. System context

```mermaid
flowchart LR
    user([User]) --> spa["Browser SPA<br/>React 19 + Vite"]
    spa -->|"POST /api/plan (SSE or JSON)<br/>resume, share, feedback, evals"| api["FastAPI app<br/>app.main"]
    api --> runner["LangGraph runner<br/>app.graph.runner"]
    runner --> llm["LLM providers<br/>Cerebras, then Groq (production)<br/>Ollama / scripted fake (dev, tests)"]
    runner --> data["Data providers"]
    data --> gp["Google Places API (New)"]
    data --> snap["OSM snapshot (Bangalore)"]
    data --> om["Open-Meteo<br/>geocoding + forecast"]
    data --> tav["Tavily + event pages<br/>(schema.org JSON-LD)"]
    data --> fx["Frozen-world fixtures"]
    api --> store[("Store: SQLite or Postgres<br/>plans, runs, rate limits,<br/>feedback, eval summary")]
    runner --> ckpt[("LangGraph checkpointer<br/>memory / SQLite / Postgres")]
    api --> obs["Observability<br/>JSON logs, Sentry, LangSmith"]
```

The browser talks only to the FastAPI origin, which also serves the built SPA, so there is no CORS. Provider keys
stay on the server (`app.config.Settings`; secrets are `SecretStr`).

## 3. Component architecture

| Component | Location | Responsibility |
|---|---|---|
| Contracts | `backend/app/domain/schemas.py`, `backend/app/providers/base.py` | Pydantic models shared by every layer; provider protocols |
| Domain core | `backend/app/domain/*` | Time, constraints vocabulary, categories, pacing, sun, evidence, travel, costs, schedule, validator, scoring, deterministic planner, infeasibility, rules parser |
| Providers | `backend/app/providers/*` | Google Places, OSM snapshot, Open-Meteo, Tavily + JSON-LD events, frozen-world fixtures, fault wrappers |
| Pipeline and baseline | `backend/app/pipeline.py`, `backend/app/baseline.py` | Deterministic steps shared by the graph (geocode, context, searches); a graph-free no-LLM planner used by the CLI and tests (evals and the API run the baseline through the graph) |
| Trace | `backend/app/trace.py` | One `Tracer` that emits `TraceEvent`s for nodes and tools |
| LLM layer | `backend/app/llm/*` | `models.yaml` registry, provider parameters, fallback chains, refusal detection, per-model sliding-window rate limiter, usage and cost, scripted fake model |
| Graph | `backend/app/graph/*` | `StateGraph` with 12 nodes, 5 agent tools, prompts, the runner (streaming, interrupt/resume, deadline) |
| API | `backend/app/api/*`, `backend/app/main.py` | 6 endpoints, SSE framing, middleware (security headers, body limit, error shield), SPA serving |
| Storage | `backend/app/store/*`, `backend/app/checkpoint.py` | SQLite/Postgres store, share-link redaction and rehydration, retention sweeper, checkpointer factory |
| Observability | `backend/app/observability.py` | JSON logging, Sentry init, LangSmith env gating |
| Frontend | `frontend/src/*` | Planner, shared plan, evals leaderboard, how-it-works pages; SSE client; trace, checks, plan views |
| Eval harness | `backend/evals/*`, `eval_cases.json` | Case loader, independent assertions, metrics, optional judge, runner, report |
| Ops | `Dockerfile`, `docker-compose.yml`, `render.yaml`, `.github/workflows/ci.yml`, `Makefile` | Image, local compose, Render blueprint, CI, developer commands |

## 4. Build phases

| Phase | Commits | What it delivered |
|---|---|---|
| 0. Scaffold and contracts | `5086126`, `624d9b4` | Plan v2, eval spec, engineering guide; Pydantic contracts, provider protocols, settings; timeutil, constraint vocabulary, categories, pacing; stubs |
| 1. Deterministic core | `a461315` (pre-verification checkpoint), `00142cd` (verified) | Evidence, travel, schedule, costs, validator, scoring, fallback planner, infeasibility; rules parser and clarification policy; providers; baseline and CLI. `a461315` also contained the first API/storage and frontend. `00142cd` fixed D-01..D-27 and R-1..R-12 from [PHASE1.md](verification/PHASE1.md) (currency conversion and `CURRENCY_MISMATCH`, diet negation, dotted times, astronomical sunset, meal windows, Plan B rules) |
| 2. Agent and product | `6b75075` | LangGraph graph with interrupt/resume, agent loop, ledger, caps, deadline, validate and repair, fallback, finalize; model registry (Claude, Ollama, fake; Gemini/Grok placeholders); API fixes from [PHASE2.md](verification/PHASE2.md) (bounded `available_time`, `relaxed_constraints`, resume-safe rate limits, retention sweeper, keep-alive comments, IPv6 /64 keys, per-DB checkpoint files, single resume per thread, checked-draft salvage) |
| 3. Evals, hardening, docs | `fe378a3` (ops), `053ba54` (evals) | Dockerfile, compose, Render blueprint, CI, Makefile, `.env.example`; eval harness, first results and [EVAL_REPORT.md](EVAL_REPORT.md); eval-driven fixes (LLM constraints need textual support, Ollama `num_predict` cap, salvage on text-only endings, gazetteer-first live geocoding, distances in the prose check). The first HLD/LLD/README followed in `bcd5695` |
| 3b. Hardening | Frontend `28d963d` (merge `93c67ad`); evals `c27314b` (merge `b99e885`), then `7d56159`; backend `f3f6a15`, `ae3786c`, `8a19d97` (merge `14a871e`) | Fixes for the issues found while documenting. Backend: Place Details with hours and rating for share links, `/api/health` 503 on DB failure and a `places` status, `httpx.Timeout` with a 4 s connect limit everywhere, concurrent event pages under a 15 s budget, robots.txt cache with expiry, one ERROR log for a rejected Maps key, atomic multi-limit rate checks, newest eval summary served, currencies, fares and price bands for every gazetteer country, known-fault list. Evals: `JUDGE_MODEL` default judge, record schema 2, wider prompt hash, dirty-tree resume key, harness-error records, `backend/.env` loading, cost per plan and per run, `--max-cost-usd`. Frontend: owned aborts, legs matched by ID, shared-plan focus, honest calendar status, per-currency digit grouping |
| 4. Production models and call budget | `1eacd74`, `9a3ecae` (models); `9aa9144`, `1690469`, `5643543` (fewer calls, rate limits); `95533d0`, `f0389e2`, `ba6a307`, `068aa3e` (evals) | Cerebras `gpt-oss-120b` as primary and Groq `openai/gpt-oss-120b` as fallback (Gemini pinned but unused); a call budget of two or three model calls per plan: batched `search_places` (1-4 queries, optional events), `submit_plan` returning the schedule and quantitative fixes, `check_plan`/`estimate_travel` optional, no LLM parse for structured requests; a per-model sliding-window rate limiter and per-model turn cap; rate limits reported apart from outages; eval `--pace-s` and marking of rows measured with earlier prompts |

## 5. End-to-end flows

### 5.1 New plan over SSE

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as FastAPI routes
    participant P as produce_events task
    participant R as stream_plan (runner)
    participant G as Graph nodes
    participant S as Store
    B->>A: POST /api/plan (Accept text/event-stream)
    A->>S: per-IP sliding limits, daily cap
    A->>P: create background task
    A-->>B: 200 text/event-stream
    P->>R: stream_plan(request)
    R-->>P: run_started
    R->>G: astream(stream_mode custom and updates)
    G-->>R: node_started, tool_called, tool_completed, validation, degraded
    R-->>P: TraceEvents in order
    P-->>B: data frames (keep-alive comment after 10 s of silence)
    G-->>R: node_completed finalize (run metrics), result
    P->>S: save redacted plan (share_id), record run
    P-->>B: result event with share_id
```

The planner task is independent of the HTTP connection: if the client disconnects, the run still reaches
`finalize` and is persisted. JSON mode (`Accept` without `text/event-stream`) runs the same code and returns the final
`PlanResult` (the request awaits a shielded task, so a cancelled request does not cancel the run).

### 5.2 Clarification and resume

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as FastAPI
    participant R as Runner
    participant G as Graph
    participant C as Checkpointer
    B->>A: POST /api/plan "plan something fun"
    A->>R: stream_plan
    R->>G: guard_input, parse_preferences, resolve_location, check_clarity
    G->>G: ask_clarification calls interrupt(questions)
    G->>C: checkpoint thread (user input and geocoder matches only)
    R-->>B: result needs_clarification with thread_id and questions
    A->>A: record_paused_thread(thread_id)
    B->>A: POST /api/plan/{thread_id}/resume with answers
    A->>R: stream_plan(thread_id, resume_answers)
    R->>R: per-thread asyncio.Lock, snapshot.next must be ask_clarification
    R->>G: Command(resume=answers)
    G->>G: apply_answers, parse again, asked_before=true, plan
    R->>C: delete thread checkpoints at a terminal status
    R-->>B: result complete, fallback or no_feasible_plan
```

Clarification happens before any place search, so a paused checkpoint holds only user input and geocoder matches.
At most one round is asked; after it the policy applies visible defaults. Abandoned paused threads are deleted by
the retention sweeper after 24 hours.

### 5.3 Fallback paths

```mermaid
sequenceDiagram
    participant G as plan_agent
    participant T as tools
    participant V as validate_plan
    participant F as fallback_planner
    participant X as explain_infeasible
    participant Z as finalize
    G->>T: one batched search_places call (check_plan, estimate_travel optional)
    T->>G: ToolMessages, ledger grows
    G->>T: submit_plan
    T->>V: pending submission
    V-->>G: violations as submit_plan error (repair, up to 3 submissions)
    Note over G,F: timeout, refusal, provider error, turn cap, second text-only turn,<br/>repairs exhausted, or under 12 s left
    G->>F: agent_stop reason
    F->>F: reuse last error-free check_plan draft if the stop allows salvage
    F->>F: else plan_deterministic over the ledger, then default searches
    F->>Z: fallback / deterministic plan (re-validated)
    F->>X: nothing feasible
    X->>Z: no_feasible_plan with binding constraints and one-tap relaxations
```

## 6. The LangGraph graph as built

Generated from `build_graph()` in `backend/app/graph/build.py` (`get_graph().draw_mermaid()`); dashed edges are
conditional (`follow_route` reads `state["route"]`).

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

| Node | File | Responsibility |
|---|---|---|
| `guard_input` | `nodes/guard.py` | NFKC normalisation, control-character stripping, `available_time` clamped to 30-720 min, injection and out-of-scope flags (never rejects) |
| `parse_preferences` | `nodes/parse.py` | Rules parse always; LLM structured parse only for free-text requests (not for structured requests or the baseline); `merge_parse` (request fields > LLM > rules, constraints only added) |
| `resolve_location` | `nodes/clarify.py` | Geocode the city; `RetryPolicy` with 2 attempts on retryable provider errors |
| `check_clarity` | `nodes/clarify.py` | Up to 2 blocking questions; routes to clarification, context, or `finalize` (city missing or not found after asking, lookup failed) |
| `ask_clarification` | `nodes/clarify.py` | `interrupt()`; on resume folds answers into the request with `apply_answers` |
| `build_context` | `nodes/context.py` | `apply_defaults`, coming Saturday, window, weather and sunset, pacing, enforced and original constraints; baseline or under 15 s left goes to the fallback |
| `plan_agent` | `nodes/agent.py` | One model turn with 5 tools under a per-model turn cap; one nudge after a text-only turn; failures (including rate limits) route to the fallback |
| `tools` | `nodes/execute.py` | Executes tool calls with caps and argument validation, grows the ledger, hands `submit_plan` to validation |
| `validate_plan` | `nodes/validate.py` | Validates a submission from ledger records; repair, accept, or fall back |
| `fallback_planner` | `nodes/fallback.py` | Checked-draft salvage, else beam-search planner over the ledger, then default searches |
| `explain_infeasible` | `nodes/fallback.py` | Binding constraints, cheapest budget, minimum time, relaxations |
| `finalize` | `nodes/finalize.py` | Re-validates any draft, builds `PlanResult`, emits run metrics and the single `result` event |

## 7. Design principles

| Principle | How the code enforces it |
|---|---|
| Deterministic guardrails around a model | The model never computes a price, time, address or verdict. `validate_plan` recomputes schedule and totals from ledger records; `finalize` re-validates every draft and replaces a failing one with `no_feasible_plan` |
| Numbers only from data | `PROSE_NUMERIC_CLAIM` rejects currency amounts, durations, distances and clock times in `why`, `tradeoff`, `summary_note` and omission reasons (place names from the ledger are blanked first). Template prose in the fallback planner is checked with the same function |
| Unknown is never yes | `Evidence.CONFIRMED` is required for diet and wheelchair (`LIKELY`/`UNKNOWN` fail with `DIET_UNVERIFIED`/`ACCESSIBILITY_UNVERIFIED`); an indoor/outdoor constraint needs a known `indoor` value (unknown is `SETTING_CONFLICT`). Unknown hours are `HOURS_UNVERIFIED` (an error for live-music venues, theatres and cinemas) |
| Grounding | The ledger holds every candidate any tool returned in this run; unknown IDs are `UNKNOWN_ID`. Plan B must also be a ledger ID |
| Append-only messages | `messages` uses `add_messages`; AIMessages are stored unchanged (Claude thinking blocks), every tool call gets one ToolMessage, repairs arrive as the `submit_plan` error result |
| Constraints only tighten | Original constraints are mapped from the raw request and always enforced (`CONSTRAINT_DROPPED` warns if a parser lost one). LLM-proposed constraints are kept only with textual support in the user's words |
| Prompt boundary | Static system prompts; user text and tool data travel as JSON in Human and Tool messages; event descriptions are never stored |
| Honest degradation | Each fallback or missing source is a visible `degradations` entry and a `degraded` trace event |

## 8. Model layer and provider strategy

All models are listed in `backend/app/llm/models.yaml` and built with `init_chat_model`, with `ChatCerebras` directly
for Cerebras (no `init_chat_model` provider), or as the scripted fake. The run's model is `PRIMARY_MODEL` (default `ollama:qwen3.5:9b` in `config.py`; `render.yaml` sets
`cerebras:gpt-oss-120b` with `FALLBACK_MODEL=groq:openai/gpt-oss-120b`). An optional `FALLBACK_MODEL` is chained with `with_fallbacks`; the primary then gets
60% of each call's time budget. After the chain, the deterministic planner is the last resort.

| Registry ID | Kind | Key settings | Status |
|---|---|---|---|
| `cerebras:gpt-oss-120b` | api | temperature 0, `max_tokens` 8192, `reasoning_effort` parse low / agent medium / judge medium, `max_retries` 0 (a 429 goes straight to Groq), `request_timeout_s` 60, client-side limit 5 requests per 60 s, at most 4 agent turns; built with `ChatCerebras` | **Production primary.** Free Trial: card on file, $5 credit for 30 days, 5 RPM, 30K uncached TPM, 1M tokens/day |
| `groq:openai/gpt-oss-120b` | api | temperature 0, `max_tokens` 8192, `reasoning_effort` parse low / agent medium / judge medium, `reasoning_format` hidden, `max_retries` 1, `request_timeout_s` 60, client-side limit 30 requests per 60 s; $0.15 / $0.60 per Mtok | **Production fallback.** Free tier: 30 RPM, 8K TPM, 200K tokens/day (measured: the third ~2.5K-token call within a minute gets HTTP 429) |
| `google_genai:gemini-3.6-flash`, `google_genai:gemini-3.8-flash` | api | default temperature, `max_tokens` 8192, `max_retries` 3 (3.6) / 1 (3.8), `request_timeout_s` 60; $0.75 / $3.75 per Mtok (paid, through 2026-12-31) | Pinned but not used: on 2026-11-03 both returned 503/504 "high demand" ("overloaded") during smoke tests |
| `anthropic:claude-opus-5-5` | api | `max_tokens` 16000, `output_config.effort` parse low / agent medium / judge medium, no sampling params, `json_schema` structured output, cached system prompt, refusal fallbacks; $4 / $20 per Mtok | Optional, not configured |
| `xai:<pending>` | api | `pending: true` | Placeholder; raises `PendingModelError` |
| `ollama:qwen3.5:9b` | local | `num_ctx` 32768, `num_predict` 2048, temperature 0, `keep_alive` 10m, `reasoning` false, function-calling structured output | Dev/eval only |
| `ollama:gemma4:e4b` | local | Same as qwen | Dev/eval only |
| `ollama:granite4:tiny-h` | local | `reasoning` unset, schema structured output | Dev/eval only |
| `fake:agent`, `fake:secondary`, `fake:primary_503` | fake | Scripted model with injectable faults | Offline tests and the `offline` suite |
| `baseline:deterministic` | baseline | No chat model | Floor row in every report |

Timeouts per call: api 45 s per agent turn and 15 s per parse; local 150 s and 25 s. Each call is also capped at
"remaining run time minus 12 s".

**Call budget.** The free tiers limit requests per minute, so a plan aims for two or three model calls: the LLM
parse runs only for free-text requests; the agent sends one batched `search_places` call (up to four queries, events
included on request), then `submit_plan`, which validates server-side and, on failure, returns the computed schedule
and a fix line with exact minutes and replacement candidates. `check_plan` and `estimate_travel` remain but the
prompt tells the model to skip them.

**Rate limiting.** Each model with `rate_limit` in `models.yaml` gets a process-wide sliding-window limiter
(`app/llm/ratelimit.py`) attached as the chat model's `rate_limiter`, so parse, agent and judge calls share it. A call
waits for a free slot when its time budget allows; otherwise it fails at once with a 429-like error, `with_fallbacks`
moves to Groq, and when the error that ends the chain is a rate limit the run reports "The AI provider's rate limit
was reached; switched to the rule-based planner" (eval category `provider_rate_limited`). Wait time is reported as the run metric `rate_limit_wait_ms`.

## 9. Data sources and their terms

| Source | Use | Implementation notes and limits |
|---|---|---|
| Google Places API (New) | Live places (`PLACES_PROVIDER=google`, or `auto` with a key) | Text Search with a field mask; Place Details (Enterprise SKU fields: hours, rating, price) for share-link rehydration. Google display fields are never stored: share records keep place IDs, computed fields and coordinates (30-day TTL), and re-fetch name, address, rating, hours and the Maps link on view. A rejected key is logged once at ERROR and reported by `/api/health`. Not used for model evals |
| OSM snapshot | Keyless places for Bangalore (`osm_snapshot`, `auto` without a key) and the fallback when the live provider fails | 41 curated places from Overpass, retrieved 2 Nov 2026, ODbL, "© OpenStreetMap contributors" on every candidate with `last_checked`. Costs are category estimates |
| Open-Meteo | Geocoding (gazetteer first) and daily forecast | Keyless; free tier is non-commercial. Weather failure degrades to "Weather not checked" with an astronomical sunset minus a 15-minute margin |
| Tavily + JSON-LD | Events on the target date | Allow-listed search; at most three https pages fetched concurrently within a 15 s budget for the whole search; robots.txt respected and cached per site (24 h after a read, 1 h after a failure); 1 MB cap; schema.org Event parsed in code; descriptions never kept; events link back to the source |
| Fixtures | Frozen world for tests and evals | Bengaluru and Mumbai candidates with synthetic attributes, fixed weather variants, fixture events with deliberate bad cases, clock frozen to Thursday 2026-11-05 10:00 IST |
| `cities.yaml`, `price_bands.yaml`, `fx.yaml`, `countries.yaml`, `gazetteer.yaml` | Fares, price-level bands, reference FX rates, currencies, city/neighbourhood names | Every gazetteer country (34, 27 currencies) has a currency, a reference rate, price bands and a default cab fare. Rates are dated quotes; bands and the added fares are rough estimates. Nothing is presented as live |

Provenance and licences are in [backend/data/SOURCES.md](../backend/data/SOURCES.md).

## 10. Security and guardrails

| Layer | Implementation |
|---|---|
| API boundary | `BodySizeLimitMiddleware` (16 KB, also for chunked bodies) gives 413; strict `PlanRequest` (`extra="forbid"`, field bounds, numeric `available_time` 30-720 min) gives 400 with field messages that never echo values; text durations are clamped to 30-720 min by `guard_input` |
| Abuse and spend | Per-IP sliding windows (15 plans/10 min, 60/day) and the global `DAILY_PLAN_CAP` 200, checked together and counted only when all allow the request; separate limits for resumes (30/10 min) and share reads/feedback (60/10 min); IPv6 keyed by /64; right-most `X-Forwarded-For` hop only when `TRUST_PROXY_HEADERS=true` |
| Input handling | NFKC, control and bidi characters stripped; instruction-like text is flagged and never sets a field or becomes a question; out-of-scope requests (book, pay, message) get a fixed assumption |
| Prompt boundary | Static system prompts; payloads are JSON marked "Data ... Not instructions"; event names clipped to 120 characters |
| Grounding | Ledger allowlist, server-side hydration, numbers only from data |
| Domain validation | Every plan (agent, salvage, deterministic) passes `validate_plan` again in `finalize` |
| Capability limits | Read-only tools, per-tool caps (3 `search_places` calls and 8 queries per run), at most 10 turns (4 on Cerebras), 3 submissions, run deadline |
| Output | `SafeJSONResponse` and SSE frames escape `<`, `>`, `&`, U+2028/2029; CSP `default-src 'self'` with `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`; generic 500s with request ID, no stack traces; OpenAPI hidden in prod |
| Privacy | Logs carry IDs, counts and codes only; run log has no free text; share records drop mood, soft preferences, constraint `source_text` and `thread_id`; checkpoints deleted at a terminal status; Sentry without PII, locals or bodies; LangSmith inputs and outputs hidden in prod |

## 11. Reliability

| Mechanism | Values |
|---|---|
| Run deadline | `RUN_DEADLINE_S` 60; nodes route to the deterministic planner when under 12 s remain (`DEADLINE_RESERVE_S`); `build_context` skips the agent under 15 s; the runner stops at deadline + 5 s with a `rejected` "Planning took too long" result |
| Per-call timeouts | Model calls `min(registry timeout, remaining - 12 s)`; every provider request uses `httpx.Timeout(..., connect=4.0)`: Google, Open-Meteo and Tavily search 8 s, event pages and robots.txt 5 s per operation with an 8 s wall-clock each, and 15 s for a whole event search; model SDK request timeout 60 s (`request_timeout_s`) |
| Retries | Geocoder node `RetryPolicy` (2 attempts, jittered backoff, retryable `ProviderError` only). No other HTTP retries apart from the model SDKs' retries (`max_retries` 1; 0 for Cerebras, 3 for Gemini 3.6) |
| Circuit behaviour | Per run, not per process: the first live Places failure switches the run to the snapshot (`places_mode` live -> snapshot -> unavailable); an events failure marks `events_failed` and is not retried. There is no cross-run circuit breaker; the Google provider remembers its last outcome (`status`) for `/api/health` only, and robots.txt failures are cached for 1 hour |
| Model fallback | Primary -> optional fallback model -> deterministic planner; refusals and rate-limit refusals (from the provider or the client-side limiter) are exceptions that move down the chain |
| Model rate limits | Per-model sliding-window limiter per process (Cerebras 5 / 60 s, Groq 30 / 60 s); a call waits only if the wait fits its budget (the primary's share of the turn when a fallback follows) |
| Salvage | A draft that passed `check_plan` without errors is re-validated and used (`complete`, `agent`) when the loop stops for time, the turn cap or a text-only ending |
| Degradations | Parser, agent stop reasons (including the provider rate limit), model switch, snapshot use, places unavailable, events unavailable, weather unavailable; each is a visible string and a `degraded` event |
| Shutdown | In-flight runs are drained for up to `RUN_DEADLINE_S` + 10 s; Docker `stop_grace_period` and Render `maxShutdownDelaySeconds` are 75 s |
| Concurrency | One resume per paused thread per process (`asyncio.Lock`); later or concurrent callers get `rejected` |

## 12. Observability

* **Logs:** one JSON object per line with `ts`, `level`, `logger`, `msg`, `request_id`, `run_id` and caller extras
  (counts, durations, codes). Exceptions log type and message only.
* **Trace:** every run streams `TraceEvent`s; the `finalize` `node_completed` event carries run metrics (usage,
  rate-limit wait, tool calls, protocol errors, submit attempts, repairs, nudges, turns).
* **Run log:** `runs` table (status, mode, model, latency, tokens, cost, tool counts, violation codes, submit
  attempts, degradations), kept 90 days.
* **Sentry:** only with `SENTRY_DSN`; release = git SHA, `send_default_pii=False`, no locals, no request bodies.
* **LangSmith:** only with `TRACING_ENABLED=true` and an API key; project `saturday-planner-<env>`; inputs and
  outputs hidden in prod.
* **Health:** `GET /api/health` reports DB status, version, env, primary model, resolved providers and the live
  places status (`unknown`, `ok`, `auth_error`, `unavailable`); it answers 503 when the DB ping fails.
* **Key problems:** a Google key rejection (401, 403, or 400 `API_KEY_INVALID`) is logged once per process at ERROR.

## 13. Deployment topology and CI/CD

```mermaid
flowchart LR
    gh["GitHub main"] --> ci["GitHub Actions CI<br/>backend, postgres, frontend, docker smoke"]
    ci -->|checks pass| render["Render web service<br/>Docker, Singapore, starter plan"]
    render --> neon[("Neon Postgres<br/>store + checkpoints")]
    render --> apis["Cerebras, Groq, Google Places,<br/>Open-Meteo, Tavily"]
    render --> sentry["Sentry / LangSmith (optional)"]
```

* **Image:** three stages (Node 22 builds the UI; uv installs locked dependencies without dev tools; Python 3.12
  slim runtime as uid 1000 with only `/app/var` writable). One uvicorn worker, `--proxy-headers`, image
  `HEALTHCHECK` on `/api/health` (503 when the database is unreachable).
* **Render:** `render.yaml` defines one Docker web service, `autoDeployTrigger: checksPass`, health check path, prod
  limits and `sync: false` secrets (`DATABASE_URL` is the Neon connection string).
* **Local:** `docker compose up` (SQLite volume) or `--profile postgres` with `COMPOSE_DATABASE_URL=postgresql://planner:planner@postgres:5432/planner` (Postgres 17); Ollama stays on the host.
* **CI:** on push and PR: ruff, mypy, pytest and the offline eval suite; Postgres store and checkpointer tests
  against a service container; frontend typecheck, lint, Vitest and build; Docker build plus a container smoke test
  (health, SPA, headers, plan JSON and SSE, share link, 400). A manual `workflow_dispatch` job runs model evals with
  repository secrets and uploads results.

## 14. Evaluation strategy

`eval_cases.json` is the behaviour spec. The harness in `backend/evals/` runs every case through the real graph
on the frozen world (fixture places, weather, events and clock), checks hard assertions with code that recomputes
cost, time, hours, evidence, pacing, daylight, Plan B, prose and grounding from the raw fixture files, and writes one
JSONL record per run under `backend/evals/results/<date>/`. `python -m evals.report` builds
[EVAL_REPORT.md](EVAL_REPORT.md) and `backend/evals/results/summary.json`, which `GET /api/evals/summary` and the
`/evals` page serve. Suites: `offline` (scripted fake model and injected faults, run in CI), `models` (the
baseline and real models) and `live` (the production model on real providers, five cases, reported separately).
Baseline, API and local rows are reported as separate populations, holdout cases separately from dev cases.
An optional LLM judge (`JUDGE_MODEL` by default for the `models` and `live` suites, `--judge-model none` to turn it
off) scores plan quality on a versioned rubric; hard checks never depend on it. Records use schema 2 (older records
are upgraded on read), harness exceptions are recorded as failed runs, cost is reported per plan and per run, and
`--max-minutes`/`--max-cost-usd` budgets stop a run early for a later `--resume`; `--pace-s` spaces runs for
request-limited free tiers. Rows measured with an earlier version of the agent's prompts and tools (a different
prompt hash) are marked † in the report and "earlier prompts" on `/evals`, since they are not like-for-like with
current rows.

Results: [EVAL_REPORT.md](EVAL_REPORT.md) states its generation time, the sessions and git SHA each row
came from, and which rows predate the current prompts. Read it for the numbers.

## 15. Known limitations and risks

| Area | Limitation |
|---|---|
| Live data | Live Google Places, Tavily, Postgres/Neon and the Render deploy were not exercised by the verifiers (no keys or services); see PHASE1 §6 and PHASE2 §7. The keyless Open-Meteo forecast was exercised once by the docs verification ([DOCS.md](verification/DOCS.md)) |
| Coverage | The keyless snapshot covers Bangalore only; elsewhere a Places outage gives `no_feasible_plan`. Fixtures cover Bengaluru and Mumbai |
| Local models | Small Ollama models often fall back (text-only endings, timeouts); see the report's error categories |
| Single instance | Resume locks, compiled-graph cache, model rate limiters and the in-memory default checkpointer are per process; multi-instance deployments need a shared claim |
| Estimates | Fares, price bands and FX rates are dated estimates; the Indian auto tariffs were checked against news reports only |
| Crowds | "Likely busy" is a heuristic from popularity and busy windows, not live data |
| Rate limits | Fail open when the store errors (logged) |
| Terms | Passing Google Places results to a third-party LLM at inference time is a grey area (plan §6); mitigations: no storage of display fields, hidden LangSmith IO in prod, evals only on the frozen world |
| Judge | No judge model has run in the committed results, so plan quality beyond hard checks is unmeasured there |
| Events | A search returns only the pages that arrived within its 15 s budget, and a failed robots.txt fetch disables that site for an hour |
| Rough data | Price bands and cab fares for the currencies added in the hardening pass are hand-set estimates, not checked against venues or tariffs |
| Free-tier model limits | Measured on the free tiers: Cerebras allows 5 requests per rolling 60 s (the next one gets HTTP 429 with a retry-after of about 58 s), 150 per hour, 30K tokens per minute and 1M tokens per day; Groq's free tier allows 8K tokens per minute and 200K tokens per day. A plan can use up to five Cerebras requests (the parse call and four agent turns), the whole per-minute window, so concurrent plans fall to Groq and then to the rule-based planner, and a full eval run needs pacing (`--pace-s`) or a paid tier |
| Rate limiter scope | The client-side limiter only counts its own process; several workers or instances (or other clients of the same key) can still hit the provider's 429, which then falls through to the fallback model |

## 16. Plan vs implementation

| Plan (IMPLEMENTATION_PLAN.md) | Code |
|---|---|
| Nodes `resolve_location`, `get_weather`, `compute_window` | `resolve_location`, then one `build_context` node (defaults, date, window, weather, sunset, pacing); `get_weather` is a trace tool event inside it |
| `guard_input` rejects invalid input | Bounds are enforced by the API (`PlanRequest`, 400; body limit, 413). `guard_input` only normalises, clamps `available_time` with an assumption and flags injection or out-of-scope text; planning continues |
| Out-of-scope requests get a fixed reply | A plan is still produced, with the assumption "I can't book, pay or contact anyone — here's a plan you can act on" |
| `tools` is a `ToolNode` wrapper | Custom executor `run_tools` (caps, argument models, protocol-error accounting); `submit_plan` is answered by `validate_plan` |
| LLM parse with regex parser as fallback | Rules parse always runs; the LLM parse is merged (`merge_parse`), LLM constraints need textual support and LLM interests must be grounded |
| `RetryPolicy` on every node calling external services | Only `resolve_location` |
| HTTP retries for GETs, 60 s circuit breaker after 3 failures, weather cache 1 h, events cache 6 h | Not implemented; per-run sticky provider modes instead. Only robots.txt is cached (24 h after a read, 1 h after a failure) |
| Heartbeat `TraceEvent` every 10 s | SSE comment `: keep-alive` after 10 s of silence; the `heartbeat` type exists in `TraceType` but is never emitted |
| Per-IP 5 plans/10 min and 30/day | 15/10 min and 60/day, two-window sliding estimate, checked together with the daily cap and counted only when all allow; resumes and share reads have their own limits |
| Production model from `models.yaml` `production.primary` | Runtime uses `PRIMARY_MODEL` (config default `ollama:qwen3.5:9b`; `render.yaml` sets Cerebras with the Groq fallback); `production.primary` is read by the eval runner |
| Claude Opus 5.5 as the default production model | Cerebras `gpt-oss-120b` (Free Trial) primary and Groq `openai/gpt-oss-120b` fallback; Claude remains an optional registry entry |
| Tools: single-intent `search_places` (cap 6), `check_plan` before submitting, one LLM parse per request | Batched `search_places` (1-4 queries per call, 3 calls and 8 queries per run, optional events in the same call); `check_plan` and `estimate_travel` optional and discouraged; `submit_plan` returns the computed schedule and quantitative fixes; no LLM parse for structured requests |
| At most 10 model turns | 10, lowered per model (4 for Cerebras); plus a client-side per-model rate limiter |
| Gemini and Grok pinned in the registry | Two Gemini Flash models pinned but unused; xAI is a `pending: true` placeholder |
| Validator code list | 31 codes, including `HOURS_UNVERIFIED`, `PLAN_B_INVALID`, `CURRENCY_MISMATCH`, `LOCATION_APPROXIMATE`, `IDLE_WAIT`, `PACING_RELAXED`; `CONSTRAINT_DROPPED` is a warning (the constraint is still enforced) |
| Budgets in the user's currency | Budgets are converted to the location currency with dated reference rates (`data/fx.yaml`); costs in another currency are `CURRENCY_MISMATCH` |
| Plans table stores IDs and computed fields | Stores a redacted `PlanResult` JSON: Google display fields removed, Google names in prose tokenised, rehydrated with `PlacesProvider.get` on view |
| Place Details with the Pro field mask for share links | The details mask adds hours, rating and price (Enterprise SKU), so re-opened stops keep hours and rating; only display fields are copied back |
| Snapshot of ~30 places | 41 places |
| `PLACES_PROVIDER=google\|osm_snapshot\|fixture`, `EVENTS_ENABLED` | `auto\|google\|osm_snapshot\|fixture`; `EVENTS_PROVIDER=auto\|tavily\|fixture\|none` |
| Low energy: at most one leg over 15 min (no rule stated for medium/high) | `max_long_legs` 1 / 2 / 3 for low / medium / high |
| Agent never uses a draft it did not submit | Checked-draft salvage (D-P2-10) |
| CI lints with eslint and deploys to Render | oxlint; Render deploys itself after checks pass (`autoDeployTrigger: checksPass`); Playwright e2e is not in CI |
| Preview environments for PRs | Not enabled (commented in `render.yaml`) |
| Eval results loaded into Postgres for `/evals` | `evals.report --publish` saves the summary to the store; the API serves whichever of the stored row and the bundled `backend/evals/results/summary.json` has the newer `generated_at` |
| Docs `ARCHITECTURE.md`, `GUARDRAILS.md`, `RUNBOOK.md`, ... | `docs/HLD.md`, `docs/LLD.md`, `docs/EVAL_REPORT.md`, `docs/verification/` |

## 17. Key decisions and trade-offs

| Decision | Trade-off |
|---|---|
| Hybrid graph: model for judgment, code for arithmetic and verdicts | More code than a pure agent, but no invalid plan can be displayed and models can be compared on one validator |
| Rules parser always runs under the LLM parse | Duplicated extraction logic, but constraints can never be lost and the baseline shares the parser |
| Deterministic beam-search planner as fallback and baseline | Template prose is plainer than model prose; it guarantees an answer when the model fails |
| Frozen world for all model evals | Scores describe a small synthetic world, not live data quality; no Google content is used to test models |
| Redacted share records with rehydration | Shared plans need a Place Details call per Google stop on view, at the Enterprise SKU so hours and rating come back (rate limited per IP) |
| Health check fails only on the database | A rejected Maps key or a Places outage shows in `/api/health` `places` but keeps 200, because searches fall back to the snapshot (where it covers the city) rather than the service being down |
| SQLite locally, Postgres in production behind one `Store` protocol | Two implementations to keep in sync; CI tests both |
| SSE over `fetch` with a background producer task | Runs survive disconnects, but cannot be cancelled by the client ("Stop" only aborts the reader) |
| One process, one worker | Simple drain and in-process locks; horizontal scaling needs shared claims |
| Free-tier hosted models with a client-side limiter | No model spend, but only a few plans per minute reach the agent before the fallback chain and the rule-based planner take over |
| Batched tools and a two-to-three-call budget | Fewer model calls and smaller prompts per plan; the model gets fewer chances to look around, so `submit_plan` carries the schedule and concrete fixes instead |
