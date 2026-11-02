# Documentation verification: HLD, LLD and README

Independent verification of [docs/HLD.md](../HLD.md), [docs/LLD.md](../LLD.md) and [README.md](../../README.md)
against the code, plus a fresh-clone run of the README quick start. The verifier did not write the docs or the code.

| Item | Value |
|---|---|
| Date | 2026-11-02 |
| Docs verified | `integration` at `6655d60` (code unchanged since `14a871e`) |
| Fixes | `66cd2a0` (documentation only: README.md, docs/HLD.md, docs/LLD.md) |
| Environment | macOS (arm64), uv 0.13.1, Node 26.11.1, npm 11.20.0, Chrome for mermaid rendering |
| Not used | Ollama (a model eval was running on the host; every run here used `baseline:deterministic` or `fake:agent`, with `OLLAMA_BASE_URL` pointed at a dead port as a guard), API keys, Postgres, Docker |

Method:
* **Claims:** every checkable claim was extracted section by section and checked against the code by reading, grep,
  and introspection with `uv run --frozen python`. Introspected objects include:
  * `Settings.model_fields`, `CODES` and `WARNING_CODES`, `TOOL_NAMES` and `tool_schemas()`;
  * every Pydantic model in `schemas.py`, `app.routes`, and `build_graph().compile().get_graph()`;
  * the argparse parsers (`app.cli`, `evals.runner`, `evals.report`);
  * `models.yaml`, `categories.yaml`, `price_bands.yaml`, `countries.yaml`, `gazetteer.yaml` and the fixtures;
  * pytest collection, plus the TestClient for endpoint status codes and headers.
* **Commands:** behaviour claims that a run can show were also checked by running the app (section 4).
* **Wrong claims:** each was fixed in place with the correct value; its evidence is cited below.

## 1. Results by category

Counts are correct / wrong / unverifiable. "Wrong" includes incomplete statements that would mislead a reader (for
example a list missing an item). The LLD frontend row is approximate (about 230 UI strings and file facts).

### README.md

| Category | Correct | Wrong | Unverifiable |
|---|---|---|---|
| File paths and document links (features table, docs table, anchors) | 40 | 0 | 0 |
| Make targets and their descriptions | 12 | 1 | 0 |
| CLI commands and flags | 9 | 0 | 0 |
| Settings, defaults, model IDs and provider switches | 29 | 1 | 0 |
| Graph diagram (nodes and edges) | 35 | 0 | 0 |
| Agent tools, caps and limits (5 tools, 10 turns, 60 s, 2 repairs, 2 questions) | 10 | 0 | 0 |
| Data sources and counts (41 places, ODbL, 30-day shares, 10% contingency) | 7 | 0 | 1 |
| Test and eval commands, markers, eval flags, report contents | 19 | 2 | 0 |
| Deployment (Render, Neon, compose, Docker, health) | 13 | 1 | 2 |
| Behaviour confirmed by running (section 4) | 9 | 0 | 0 |
| Limitations and process (trailers on all 19 commits, placeholders, per-process locks) | 5 | 0 | 1 |
| **Total** | **188** | **5** | **4** |

### docs/HLD.md

| Category | Correct | Wrong | Unverifiable |
|---|---|---|---|
| Header, commit hashes, build phases | 15 | 1 | 0 |
| Context and components (12 nodes, 5 tools, 6 endpoints, paths) | 18 | 0 | 0 |
| Flows (SSE, clarification and resume, fallback) | 20 | 0 | 0 |
| Graph diagram edges (§6, compared with the compiled graph) | 23 | 0 | 0 |
| Node table (§6) | 14 | 0 | 0 |
| Design principles (§7) | 10 | 2 | 0 |
| Model layer (§8: every `models.yaml` parameter, prices, timeouts) | 24 | 0 | 0 |
| Data sources (§9) | 20 | 0 | 2 |
| Security (§10: limits, headers, CSP, body cap) | 24 | 0 | 0 |
| Reliability (§11: deadlines, timeouts, drain) | 19 | 1 | 0 |
| Observability (§12) | 12 | 0 | 0 |
| Deployment and CI (§13) | 16 | 1 | 1 |
| Evals (§14) | 12 | 1 | 0 |
| Limitations (§15) | 10 | 0 | 1 |
| Plan vs implementation (§16) | 27 | 1 | 0 |
| Decisions (§17) | 9 | 0 | 0 |
| **Total** | **273** | **7** | **4** |

### docs/LLD.md

| Section | Correct | Wrong | Unverifiable |
|---|---|---|---|
| Intro, contents, §1 layout (205 paths exist, descriptions, data counts) | 330 | 1 | 0 |
| §2 contracts (30 Pydantic models: fields, types, defaults, bounds, enums) | 141 | 1 | 0 |
| §3.1-3.12 domain (time, constraints, categories, pacing, sun, evidence, travel, costs, schedule, 31 validator codes and severities, checks, scoring) | 286 | 1 | 0 |
| §3.13-3.15 deterministic planner, infeasibility, rules parser | 144 | 4 | 0 |
| §4 providers (protocols, selection, Google, Open-Meteo, Tavily, snapshot, fixtures, faults, reference data) | 170 | 2 | 1 |
| §5 pipeline, baseline, trace events | 50 | 1 | 0 |
| §6 LLM layer (registry entries, params, pricing, fake model) | 44 | 0 | 0 |
| §7 graph (assembly and 23 edges, state, nodes, tools and caps, protocol errors, salvage, prompts, runner) | 170 | 4 | 0 |
| §8 API (endpoints, rate limits, SSE, middleware, lifecycle, CLI) | 149 | 2 | 0 |
| §9 storage (7 tables and every column, redaction, retention, checkpointer) | 78 | 0 | 1 |
| §10-11 observability and configuration (all 32 `Settings` fields and defaults) | 86 | 0 | 0 |
| §12 frontend (about) | 230 | 4 | 0 |
| §13 eval harness (spec, runner CLI and exit codes, record schema 2, assertions, metrics, judge, report, scripts) | 137 | 3 | 0 |
| §14 tests (1674 backend tests in 46 files, 11 deselected; 104 Vitest tests in 9 files; 11 Playwright tests) | 56 | 0 | 0 |
| §15 build, deploy, CI, Makefile | 67 | 1 | 0 |
| §16 plan vs implementation | 14 | 1 | 0 |
| **Total** | **about 2152** | **25** | **2** |

### All three documents

| | Correct | Wrong | Unverifiable |
|---|---|---|---|
| **Total** | **about 2613** | **37** | **10** |

## 2. Wrong claims and the fixes applied

All fixes are in `66cd2a0`. Line numbers are those before the fix.

### README.md (5)

| Line | Was | Now | Evidence |
|---|---|---|---|
| 123 | `make lint` runs "ruff and oxlint" | "ruff, tsc and oxlint" | `frontend/package.json` `lint`: `tsc -b && oxlint --deny-warnings src` |
| 124 | Live tests "need `RUN_LIVE=1` with keys, or a Postgres `DATABASE_URL`" | Live tests need `RUN_LIVE=1`. Google and Tavily also need their keys; Postgres tests need a Postgres URL | `backend/tests/providers/test_open_meteo.py:264` (no key needed), `test_google_places.py:408`, `test_events_web.py:341`, `tests/store/test_store_postgres.py:26` |
| 138 | "Cost is shown per plan and per run" | The generator shows both, but the committed report predates this and shows cost per run only until `make eval-report` | `docs/EVAL_REPORT.md:9` (Cost/run column only, generated at `053ba54`); `backend/evals/report.py:319` (Cost/plan added in `c27314b`) |
| 148 | `sync: false` list omitted `JUDGE_MODEL`, `GOOGLE_API_KEY` and `XAI_API_KEY` | Full list; `TRACING_ENABLED` is `false` in `render.yaml` | `render.yaml:46-60, 99-100` |
| 176 | `JUDGE_MODEL` "(the app never reads it)" | "(a `Settings` field, but only the eval runner uses it)" | `backend/app/config.py:22`; used only in `backend/evals/runner.py:277` |

Updates (correct when written, superseded by this verification):
* The "Limitations" bullet and HLD §15 now say that the live Open-Meteo forecast was exercised once (section 4, step 7).
* The documentation table links this report.

### docs/HLD.md (7)

| Line | Was | Now | Evidence |
|---|---|---|---|
| 75 | evals `c27314b`, `7d56159` (merge `b99e885`) | `c27314b` (merge `b99e885`), then `7d56159` | `git log -1 --format=%p b99e885` = `d6b7117 c27314b`; `7d56159`'s parent is `b99e885` |
| 214 | `Evidence.CONFIRMED` required for "diet, wheelchair, setting"; failures are `*_UNVERIFIED` | Diet and wheelchair use `DIET_UNVERIFIED`/`ACCESSIBILITY_UNVERIFIED`; indoor/outdoor needs a known `indoor` value (unknown is `SETTING_CONFLICT`) | `backend/app/domain/evidence.py:360-375` |
| 214 | `HOURS_UNVERIFIED` an error for "concert halls" | "live-music venues", theatres and cinemas | `evidence.py:124` (`PROGRAMME_CATEGORIES`), `validator.py:404-405` |
| 275 | "No other HTTP retries" | Adds the model SDKs' single retry | `backend/app/llm/registry.py:192, 220, 224` (`max_retries` 1) |
| 314 | `--profile postgres` (Postgres 17) | Also needs `COMPOSE_DATABASE_URL=...` | `docker-compose.yml:33-35` (otherwise SQLite) |
| 335 | Report generated "(commit `053ba54`)" | Runs on a dirty tree at `6b75075`, committed in `053ba54`; the report predates the per-plan cost column | `backend/evals/results/summary.json` `git_sha`; `docs/EVAL_REPORT.md:181-183` |
| 368 | Plan said "5 plans/10 min and 30/day, fixed windows" | "fixed windows" removed: the plan names no window type | `IMPLEMENTATION_PLAN.md:352` |

### docs/LLD.md (25)

| Line | Was | Now | Evidence |
|---|---|---|---|
| 33 | §1 layout omitted `README.md` | Added | repository root |
| 341 | `PlanResult.mode` None only for "clarification and rejected" | Also for location failures (`check_clarity` -> `finalize`); the graph-free baseline always sets `deterministic` | `backend/app/graph/nodes/clarify.py:95-96`; `backend/app/baseline.py:67` |
| 542 | `opening_status` for events | Events without a start time are `unknown` | `backend/app/domain/evidence.py:195-199` |
| 722 | Time-use term `3 * min(1, (used - 0.6) / 0.25)` | `max(0, used - 0.6)` clamp, and what `used` is | `backend/app/domain/fallback.py:538-551` |
| 759 | When nothing binds, "the two constraints" | Up to three (the reason text names two) | `backend/app/domain/infeasible.py:271, 291` |
| 769 | Relaxation label "Start at H:MM" | "Start at 3:00 PM" style (`fmt_clock`) | `infeasible.py:191, 267` |
| 798 | Only `_SOFT_RE` text becomes `soft_preferences` | Plus structured entries that are neither hard-sounding nor instruction-like | `backend/app/domain/parser_rules.py:885-904` |
| 1032 | Snapshot query "(§13.5)" | §13.8 (Scripts) | LLD headings |
| 1068 | "the 23 added on 2026-11-02" price bands | 18 added in the hardening pass; every band except INR/GBP/USD/EUR is a rough estimate | `backend/data/price_bands.yaml:39, 70-72`; 9 currencies before `f3f6a15`, 27 after |
| 1123 | `tool_failed` codes | Adds `TRANSFER_UNKNOWN` | `backend/app/graph/tools.py:558` |
| 1313 | Tool `reason` "required in the schema" | Required only for `search_places`, `search_events`, `estimate_travel` | `tools.py:95` (`required: ["stops", "summary_note"]` for `check_plan`/`submit_plan`) |
| 1354 | `schema_invalid` never uses a cap | A malformed `submit_plan` still counts as a submission | `backend/app/graph/nodes/validate.py:43, 49-50` |
| 1355 | `unknown_id` uses a cap only for `check_plan` | `submit_plan` too (every validated submission counts) | `validate.py:43, 56-57` |
| 1394 | `stream_plan` is "the single entry point for ... the CLI" | Except `baseline:deterministic` without `--trace`, which uses `run_baseline` | `backend/app/cli.py:94-101` |
| 1431 | `build_run_record(result, finalize_metrics)` | `(result, finalize_metrics, deps)` | `backend/app/api/routes.py:168, 218` |
| 1478 | "`X-Frame-Options: DENY` (unless already set)" | Each security header is added only when absent | `backend/app/api/security.py:81-84` |
| 1651 | `.oxlintrc.json` rules | Adds `react/only-export-components` warn; `--deny-warnings` makes warnings fail lint | `frontend/.oxlintrc.json:5`, `package.json` |
| 1678 | Badge "Google Maps ... OpenStreetMap with `last_checked`" | Badge "Live · Google Maps"; `last_checked` is in the attribution | `frontend/src/lib/format.ts:162-168`, `components/StopCard.tsx:22-45` |
| 1752 | "captions and scope on tables" | `scope` everywhere; captions only on the leaderboard tables | `pages/EvalsLeaderboard.tsx:58`, `pages/HowItWorks.tsx:95, 126` |
| 1753 | "(opens in a new tab)" on external links | Only on the Google Maps and WhatsApp links | `StopCard.tsx:41, 54, 100`; `PlanActions.tsx:40, 64`; `PlanView.tsx:169` |
| 1850 | Assertions import "only `app.domain.pacing.PROFILES`" | The only planner rule imported; also schema types and fixed texts | `backend/evals/assertions.py:29-38` |
| 1880 | Keyword proxies "over trade-offs, omissions and summaries" | Plus weather in a stop's `why` | `assertions.py:1238-1245, 1567-1569` |
| 1913 | `build_structured_runnable([model], JudgeVerdict, purpose="judge")` | Adds `settings` | `backend/evals/judge.py:138`; `registry.py:393` |
| 2050 | CI checks a "JSON 404" for `/api/nope` | A 404 (status only) | `.github/workflows/ci.yml:189-190` |
| 2080 | Tool `reason` "Required in the JSON schema" | Same correction as line 1313 | `tools.py:95, 119, 130, 140` |

Also: the §1 layout now lists `verification/DOCS.md`.

## 3. Unverifiable claims

| Document | Claim | Why |
|---|---|---|
| README, HLD §9 | Open-Meteo free tier is non-commercial | External terms; matches `backend/data/SOURCES.md:149` |
| README | Neon in AWS ap-southeast-1; the pooled host works | No Neon access. The code disables prepared statements (`prepare_threshold=None`), which is what the pooled host needs |
| README | Render rollback path (Events -> earlier deploy -> Rollback) | Render UI |
| README | Verification passes "encoded most defects as strict-xfail tests" | History shows xfail tests in `6b75075~1`; "most" is not measurable |
| HLD §9, §16; LLD §4.3 | Hours, rating and price fields are billed at the Place Details Enterprise SKU | External Google pricing; matches the field mask and `google_places.py:73-76` |
| HLD §13, §16 | `autoDeployTrigger: checksPass` deploys only after checks pass | Render-side behaviour; `render.yaml:27` sets it |
| HLD §15 | Passing Places results to an LLM is a "grey area" | Legal judgement |
| LLD §9.1, §9.5 | Postgres/Neon runtime behaviour (pooled host, lock order) | No Postgres available; the code matches the description |

## 4. Fresh-clone quick start

Clone: `git clone /Users/vatsalyaroy/Desktop/bitscale/psp-integration /tmp/psp_docs_verify && git checkout integration`
gave HEAD `6655d60`. Steps followed README "Quick start (macOS)" literally, with keyless providers.

| # | Step (as README says) | Result | Pass/fail |
|---|---|---|---|
| 1 | `cp .env.example backend/.env` | Copied | Pass |
| 2 | `make setup` | `uv sync --frozen` and `npm ci` (129 packages) in 1.9 s with warm caches; exit 0. npm warns that `fsevents` install scripts are not allow-listed (harmless) | Pass |
| 3 | `make test` | Backend: 1674 passed, 11 deselected in 61.8 s.<br>Frontend: 103 passed and 1 failed (`Planner.test.tsx` "streams the trace and renders the plan": `#plan-title` did not have focus); `make` exit 2.<br>Three standalone reruns: fail, pass, pass. The test is flaky (section 6, item 1) | Fail (flaky) |
| 4 | `make build` | `tsc -b && vite build` in 3.3 s; exit 0 | Pass |
| 5 | Edit `backend/.env`: `PRIMARY_MODEL=baseline:deterministic` (README: "edit keys and PRIMARY_MODEL here") | Also set `OLLAMA_BASE_URL=http://127.0.0.1:9` as a guard so nothing could reach the host's Ollama | Pass |
| 6 | `make serve` | `GET /api/health` 200 with CSP, nosniff, Referrer-Policy, `X-Frame-Options: DENY`, `X-Request-ID`, `Cache-Control: no-store`. Body: `{"status":"ok","primary_model":"baseline:deterministic","places_provider":"osm_snapshot","events_provider":"none","db":"ok","places":"ok"}` | Pass |
| 7 | `POST /api/plan` with the brief's JSON, `Accept: application/json` | 200 in 2.1 s: `complete`, `deterministic`, share ID. Three indoor stops (National Gallery of Modern Art, La Bonita Confiteria, Alliance Francaise) because the live Open-Meteo forecast said heavy drizzle with an 85% chance of rain. No degradations | Pass |
| 7b | Same request with `Accept: text/event-stream` | 1 `run_started`, 7+7 node events, 8+8 tool events, 1 `validation`, exactly one `result` | Pass |
| 7c | Share link, SPA, summary, bad input | `GET /api/plans/{share_id}` 200; `/` and `/evals` 200 (SPA); `/api/evals/summary` 200; `{"bogus":1}` gives 400 | Pass |
| 8 | `make dev` with README's frozen world (`PLACES_PROVIDER`, `EVENTS_PROVIDER`, `WEATHER_PROVIDER`, `GEOCODER` all `fixture`; `FIXED_NOW=2026-11-05T10:00:00+05:30`) and `PRIMARY_MODEL=fake:agent` | Health is the same through the Vite proxy on :5173 and direct on :8000. The plan through the proxy returned 200 in 0.37 s: `complete`, `agent`, `fake:agent`, Saturday 2026-11-07, three stops, share ID. Ctrl+C stopped both servers | Pass |
| 9 | CLI `uv run python -m app.cli plan --fixture` | `complete`, `deterministic`, three stops, eight checks pass | Pass |
| 10 | CLI `... --free-text "Indiranagar, 1500 rupees, 4 to 8pm, vegan" --fixture --json` | JSON: `complete`, `deterministic`, budget 1500 | Pass |
| 11 | CLI `... --model ollama:granite4:tiny-h ...` | Not run (Ollama) | Skipped |
| 12 | README: with the default `PRIMARY_MODEL` and no Ollama, "every run falls back ... and says so" (simulated with a dead `OLLAMA_BASE_URL`, `--model ollama:qwen3.5:9b`) | `fallback`, `deterministic`. Degradations: "AI parser unavailable; used rule-based parsing" and "The AI planner was unavailable; switched to the rule-based planner" | Pass |
| 13 | `make eval-offline` | `fake:agent` hard pass 32/32 in 55 s; exit 0. Side effect: it appends to the committed `backend/evals/results/2026-11-02/fake_agent.jsonl` (section 6, item 2) | Pass |
| 14 | `make eval-report` (run in the clone, then reverted) | Regenerated `docs/EVAL_REPORT.md` and `summary.json` from 199 records, now with the Cost/plan column | Pass |
| - | `make ollama`, `make eval`, `make docker-build`/`docker-run`, `npm run e2e` | Not run (out of scope or Ollama) | Skipped |

Wrong README instructions: none. Every quick-start command worked as described. The only failure was the flaky
frontend test in `make test`.

## 5. Mermaid

| Block | Type | Parses | Graph check |
|---|---|---|---|
| README.md line 76 | `graph TD` (agent graph) | Yes | 23/23 edges equal `build_graph().compile().get_graph().edges` (7 direct, 16 conditional; solid and dashed styles match); no missing or extra edges |
| HLD.md line 29 | `flowchart LR` (system context) | Yes | - |
| HLD.md line 81 | `sequenceDiagram` (SSE flow) | Yes | - |
| HLD.md line 110 | `sequenceDiagram` (clarification) | Yes | - |
| HLD.md line 139 | `sequenceDiagram` (fallback) | Yes | - |
| HLD.md line 166 | `graph TD` (agent graph) | Yes | 23/23 edges, identical to the compiled graph |
| HLD.md line 300 | `flowchart LR` (deployment) | Yes | - |

* **Parse check:** `@mermaid-js/mermaid-cli` 12.2.0 (`mmdc`, with the local Chrome) rendered every block to SVG,
  before and after the fixes. As a negative control, a deliberately broken diagram fails with "Parse error on line 2".
* **LLD:** it has no mermaid block. Its §7.1 edge/route table also matches the compiled graph (12 nodes, 23 edges);
  `recursion_limit(10, 3)` is 66.

## 6. Code issues found (not fixed; documentation-only mandate)

1. **Flaky frontend test.** `frontend/src/pages/Planner.test.tsx:59` asserts `toHaveFocus()` synchronously after
   `findByRole`. Focus is moved in a `useEffect` (`frontend/src/pages/Planner.tsx:61`), so the assertion races it under
   load. It failed in 2 of 4 runs here, which makes `make test` exit 2. Fix: `await waitFor(() => expect(...).toHaveFocus())`.
2. **`make eval-offline` dirties the tree.** It appends to the committed
   `backend/evals/results/<today>/fake_agent.jsonl` when run on a date that already has results.
3. **Stale committed eval output.**
   * `docs/EVAL_REPORT.md` and `backend/evals/results/summary.json` were generated before the eval-harness hardening
     (`c27314b`), so they have no Cost/plan column and no `record_schemas` key, and `/evals` serves that older summary.
   * The report says 195 records; the committed JSONL files hold 199.
   * Fix: run `make eval-report` and commit the result.
4. **LangSmith settings in `backend/.env` are applied too late without `--env-file`.**
   * Tracing is configured from the process environment at startup (`backend/app/main.py:72`,
     `backend/app/observability.py:132, 138`), before `export_provider_env()` copies `.env` keys
     (`backend/app/deps.py:52`).
   * So a plain `uv run uvicorn app.main:app` leaves tracing off and ignores `LANGSMITH_PROJECT`, which contradicts the
     comment at `config.py:32-33`.
   * `make dev`/`serve`, Docker and Render are not affected.
5. **Missing key gives an unclear error.** `PLACES_PROVIDER=google` or `EVENTS_PROVIDER=tavily` without the key fails
   with a bare `AssertionError` in `build_deps` (`backend/app/deps.py:73, 85`) instead of a configuration error.
6. **Accessibility.** Three new-tab links have no "(opens in a new tab)" text: `frontend/src/components/StopCard.tsx:41,
   54` and `frontend/src/components/PlanView.tsx:169`.
7. **"How it works" page contradicts the code** (`frontend/src/pages/HowItWorks.tsx`):
   * Line 7 says `guard_input` rejects non-leisure requests; `guard.py` never rejects.
   * Line 8 says the rules parser is "the fallback"; it always runs and is merged.
   * Line 18 says `finalize` saves the plan, writes the run record and deletes the checkpoint. That work is done by
     `persist_result` in `api/routes.py` and by `graph/runner.py`.
8. **Stale comments and docstrings:**
   * `Makefile:5` and `.env.example:7` say `config.py` does not declare `ANTHROPIC_API_KEY` and the like, but it does
     (`config.py:34-37`).
   * `docker-compose.yml:4` calls `--profile postgres` "prod-like: store and checkpoints in Postgres", but that is only
     true with `COMPOSE_DATABASE_URL`.
   * `graph/state.py:75` (`pending_submit` also holds `reason`).
   * `domain/constraints.py:27` ("first match per rule group wins").
   * `domain/schedule.py:82` (never returns `SCHEMA_INVALID`).
   * The `CODES` meanings of `TRANSFER_UNKNOWN` and `PLAN_B_INVALID` are incomplete.
   * `validator.__all__` omits helpers used elsewhere.
   * The `registry.get_spec` docstring ("whose only entry is pending").
9. **Inconsistent `mode` for the same outcome.** The graph-free baseline returns `mode="deterministic"` for
   `needs_clarification` (`backend/app/baseline.py:67`); the graph returns `None` (`graph/runner.py:307`).
10. **Minor:**
    * The CI smoke test checks only the status code of `/api/nope`, not that the body is JSON (`ci.yml:189-190`).
    * FastAPI's default 404/405 handlers use the plain JSON response class.
    * An ad-hoc `ollama:<tag>` defaults `parse_s` to 60 if `models.yaml` ever loses `timeouts.local.parse_s`
      (`registry.py:146`).

## 7. Verdict

**Signed off. After `66cd2a0`, the documents match the code.**

* **Claim audit:** about 2660 checkable claims; 37 were wrong (about 1.4%), all minor: a missing case, an
  incomplete list, an off-by-one count, a wrong cross-reference or a misattributed commit. All are corrected.
* **No wrong high-impact claims:** no endpoint, status code, setting default, rate limit, timeout, validator code,
  graph edge, tool cap, model parameter, table column, Make target or test count was wrong.
* **Quick start:** it works from a fresh clone exactly as written for keyless local use (`make setup`, `make build`,
  `make dev`, `make serve`, `/api/health`, `POST /api/plan` in JSON and SSE, the CLI, `make eval-offline`).
* **One caveat, in the code:** `make test` can fail intermittently because of the flaky focus test (section 6,
  item 1). It should be fixed before relying on `make test` as a gate.
