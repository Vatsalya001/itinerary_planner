# Phase 2 verification report

Independent verification of the agent and product layer: the LangGraph graph, model registry, FastAPI SSE/JSON API,
storage and share links, and the React UI. Run on 2 November 2026 against the working tree. The Phase 1 fix commit
(`00142cd`) landed while this was running, so every real-server flow was re-run on that HEAD; the results below come
from HEAD. `backend/evals/` and `backend/tests/evals/` were out of scope because another agent was editing them.

**Verdict: conditional pass.** The Phase 2 exit conditions are met:

* The offline graph tests are green.
* A local Ollama model (`granite4:tiny-h`) plans the brief end to end in agent mode, in 20–23 s.
* The UI works locally against the real backend, and 11 browser tests pass.

No P0 defects were found:

* No invalid plan was displayed.
* No secret or system prompt leaked.
* No injection was followed.
* Unknown IDs never reached a plan.
* Every stream had exactly one `result`.

Three P1 defects should be fixed (or decided) before the public deploy:

* **D-P2-01:** the API accepts unbounded `available_time`, and one path crashes.
* **D-P2-02:** "Drop constraint" relaxations do nothing for free-text requests, which is the UI default.
* **D-P2-03:** the default per-IP limit blocks the reviewer's first-two-minutes checklist.

All file and line references are relative to the repo root. Each backend defect has an `xfail(strict=True)` test in
`backend/tests/verification/test_phase2_api_graph.py`, and D-P2-02 also has a `test.fail()` browser test. These tests
assert the correct behaviour, so a fix makes them XPASS, and they then need un-marking.

---

## 1. Defects (prioritised)

### P1: fix before the public deploy

**D-P2-01: `available_time` is never bounded to 30–720 minutes. An out-of-range value either produces a nonsense
"complete" plan or crashes the run.**

* **Where:**
  * `backend/app/graph/nodes/guard.py:48,64-65`: `guard_input` truncates `available_time` to 40 characters but
    never checks the minutes. §4 of the plan says the guard enforces "30–720 minutes".
  * `backend/app/domain/timeutil.py:50-56`: `parse_duration` returns any integer as-is.
  * `backend/app/domain/schemas.py:68`: `available_time: str | int | None` has no bound.
* **Reproduction:**
  * `POST /api/plan` with the brief plus `"available_time": 100000` returns `complete` with
    `available_minutes: 100000`.
  * `"20 hours"` gives `1200`.
  * `"9"*40` gives a 40-digit `available_minutes`, and the server logs `TypeError: Integer exceeds 64-bit range`
    from the checkpointer's msgpack serializer.
  * The same value on a vague request (`{"free_text": "plan something fun", "available_time": "9"*40}`) needs a
    checkpoint at the interrupt. That write fails, so the user gets `rejected`: "Something went wrong while planning".
  * `"10"` (minutes) is not rejected either. It produces `no_feasible_plan`.
* **Scope:** the UI only sends dropdown values (60–720), and the free-text parser clamps to 720, so only direct API
  callers can trigger this. The brief's JSON is an advertised input format, though.
* **Tests:** `test_available_time_outside_bounds_is_rejected_or_clamped[...]` (3 cases) and
  `test_vague_request_with_huge_available_time_does_not_crash`.
* **Fix:** reject `available_time` outside 30–720 minutes with a field-level 400 (a validator on `PlanRequest`, or
  `guard_input` via the API layer). Also clamp `parse_duration` output and `Preferences.available_minutes`
  (`le=720`). Owners: contracts (schemas), API, and Phase 1 timeutil.

**D-P2-02: "Drop '<constraint>'" relaxations are a no-op when the request was free text, which is the UI's default
path.**

* **Where:**
  * `backend/app/domain/infeasible.py:217`: the patch only replaces `constraints`.
  * `frontend/src/lib/plan.ts:26-34` (`applyPatch`) keeps `free_text` unchanged.
  * `backend/app/domain/parser_rules.py:821-826` (`original_constraints`) re-derives every constraint from
    `free_text` and unions it with the `constraints` field.
* **Reproduction:**
  * Free text: "I'm in Bangalore with ₹2000 and 4 hours this Saturday. I'm tired. I'm vegan and jain, need
    wheelchair access and want to avoid crowded places."
  * The result is `no_feasible_plan` with the button "Drop 'need wheelchair access'".
  * Clicking it resubmits `{free_text, constraints: ["I'm vegan", "jain", "want to avoid crowded places"]}`.
  * The result is the same `no_feasible_plan`, still with `access:wheelchair`.
  * Budget, time and mood relaxations do work.
* **Tests:**
  * `test_drop_relaxation_drops_the_constraint_for_free_text`
  * `frontend/e2e/planner.spec.ts` "Drop <constraint> actually drops it…" (`test.fail()`)
* **Fix (pick one):**
  * Make an explicit `constraints` field authoritative over constraints found in free text. That matches §4's
    "form fields are authoritative". `CONSTRAINT_DROPPED` would then compare against the explicit list when one is
    given.
  * Add a contract field such as `relaxed_constraints: ["access:wheelchair"]` that `original_constraints` subtracts.
* **Owners:** contracts, Phase 1 parser, infeasible, and frontend `applyPatch`.

**D-P2-03: the default per-IP limit (5 plans per 10 minutes) counts resumes and relaxations, so a reviewer
following the submission checklist gets a 429 within two minutes.** This is a decision more than a bug.

* **Where:**
  * `backend/app/config.py:45`
  * `render.yaml:85-88`
  * `backend/app/api/routes.py:314-321`: `resume_plan` calls `check_limits`.
* **Reproduction:** §16 item 1 of the checklist, at production limits: the brief, a vague request plus its answer,
  a contradictory request, a tiny budget plus one relaxation tap, then another city. That is 7 POSTs, and the 6th
  gets a 429. "Stop" in the UI only aborts the client, so the server run still counts.
* **Test:** `test_reviewer_checklist_fits_default_rate_limit`.
* **Fix:**
  * Don't count `/resume` against the per-IP window, because it continues a run that was already counted.
  * Raise the per-10-minute limit to about 15 and keep the daily cap as the spend guard.
  * Or at least whitelist the review window.

### P2

**D-P2-04: run-log usage is double counted across a clarification.**

* **Where:** the `usage` state uses an add reducer (`backend/app/graph/state.py:83`) that is checkpointed at the
  interrupt. The resumed call's `run_metrics` (`backend/app/graph/nodes/finalize.py:40`) reports the cumulative
  total.
* **Effect:** the resume row repeats the first call's parse tokens.
  * Real server: clarify row 491 input tokens; resume row 10,493; an equivalent direct run 9,998.
  * Summing `runs.cost_usd` over-counts every clarified run.
* **Test:** `test_resume_usage_counts_only_its_own_llm_calls`.
* **Fix:** in `runner._run_graph`'s resume path, capture `snapshot.values["usage"]` and report the delta. The
  alternative is to stamp the pause-time usage in state.

**D-P2-05: concurrent resumes of one paused thread all run.**

* **Where:** `backend/app/graph/runner.py:196-207` checks `snapshot.next`, then acts, with no lock or claim.
* **Reproduction:** three simultaneous `POST /api/plan/{thread}/resume` all returned `complete` with three
  different share IDs, on the real server and in-process. Each one is a full model run. The per-IP limit bounds the
  spend, but the one-shot contract is broken.
* **Test:** `test_a_paused_thread_resumes_at_most_once_under_concurrency`.
* **Fix:** claim the thread atomically before resuming. Options:
  * a `resumed_threads` row with a unique key in the store;
  * an in-process `asyncio.Lock` per thread ID plus a store claim for multi-instance setups.
  Later callers get the EXPIRED result.

**D-P2-06: "Trade-offs and what I left out" lists reasons without saying what was left out.**

* **Where:** `CanonicalPlan.omitted` carries only `ref` and `reason` (`backend/app/domain/schemas.py:306,358`).
  `PlanView` hides ID-like refs (`frontend/src/components/PlanView.tsx:11,113-118`). The deterministic and
  fake-model reasons never name the place ("Left out because it isn't open long enough during your window.").
* **Effect:** the brief's own plan shows three anonymous "Left out because…" lines.
* **Test:** `test_omitted_candidates_are_identifiable`.
* **Fix:** hydrate omitted IDs to names server-side, in `validate_plan`/`build_schedule` from the ledger. Add a
  `name` field (a contract change) or rewrite the reason. Keep the `[[place:id]]` redaction for Google names in
  share records.

**D-P2-07: code-generated tool reasons are shown as "Agent's note".**

* **Where:**
  * `backend/app/pipeline.py:60` (`reason=f"forecast for {day}"`)
  * `backend/app/pipeline.py:105` (`reason=q.intent`)
  * `backend/app/pipeline.py:134` (`reason="events matching interests"`)
  * These are rendered by `frontend/src/components/TracePanel.tsx:65-69`.
* **Effect:** §5 says the trace shows the model's own reason. In a deterministic fallback, every default search
  reads as the agent's note, which misattributes code to the model.
* **Test:** `test_only_model_tool_calls_carry_a_reason`.
* **Fix:** put code-side intents in `summary` and keep `reason` for model output only. Or label notes by `node`
  (show "Agent's note" only for `node == "tools"`).

**D-P2-08: data retention isn't implemented for abandoned threads, the run log or feedback.**

* **Where:**
  * `backend/app/graph/runner.py:254-256` leaves paused threads, by design.
  * Nothing ever expires them. After the adversarial runs, `/tmp/checkpoints.db` held 5 abandoned threads (London,
    Atlantisville, contradictory, …) with the raw request text, and the file had grown to 9 MB plus a 4 MB WAL.
  * `backend/app/store/sqlite.py:163-168` only prunes rate limits and expired plans. `runs` is never pruned (§11 says
    "kept 90 days"), and neither is `feedback.reason`, which is free text.
* **Fix:** a periodic sweep:
  * delete checkpoint threads older than about 24 h (store a `paused_at` per thread, or use the checkpoint
    timestamp);
  * delete runs older than 90 days;
  * delete feedback whose plan has expired.

**D-P2-09: trace and degradation copy misleads in common cases.**

* **"Running with reduced data":** `frontend/src/components/TracePanel.tsx:140` puts this title on every
  degradation, including "The AI planner didn't submit a plan" and "…timed out", which have nothing to do with data.
* **Legitimate "no plan" answers become degradations:**
  * When the model correctly answers "no combination fits" in text, `backend/app/graph/nodes/agent.py:139-145`
    nudges, then records `AGENT_NO_SUBMIT`.
  * The infeasible screen then shows that warning above the explanation. This was seen on the "vegan + jain +
    wheelchair" request.
* **Missing labels** (`frontend/src/lib/trace.ts:32-45`):
  * The backend nodes `build_context` and the code tools `get_weather` and `resolve_location` have no labels, so they
    render as "Build context", "Get weather" and "Resolve location".
  * The label table instead lists a `compute_window` node that doesn't exist.
* **Repeated turn number:** `agent.py:119` prints the same turn number twice when a turn is skipped for time ("turn
  4: check_plan" then "turn 4: stopped (…)").

**D-P2-10: local models almost always fall back under the API's 60 s deadline. A validated draft is thrown away.**

* **Effect:**
  * gemma4:e4b fell back on 3 of 3 flows (48 s each).
  * granite4:tiny-h fell back on 3 of 4 planning flows.
  * In several runs the model had already passed `check_plan` with "draft valid" and then stalled (text-only
    replies, or a timeout). The graph discards that draft and the user waits about 48 s for a deterministic plan.
  * The models also confuse `check_plan` with `submit_plan` (the reason text was "Submit final plan…" on a
    `check_plan` call).
* **Where:**
  * `backend/app/llm/models.yaml` (local `parse_s: 60`, `agent_turn_s: 150`)
  * `backend/app/graph/nodes/agent.py:84,139-145`
  * `backend/app/graph/nodes/execute.py:204-206`
* **Fix:**
  * When the agent ends without submitting, or runs out of time, and the last `check_plan` in the run was `ok`,
    validate that draft as the submission (`mode: agent`, with a note).
  * Cap local `parse_s` at about 20 s.
  * Local models are dev and eval only per §8, so this is not a production blocker.

**D-P2-11: the LLM parse invents interests on vague requests.**

* **Where:** `backend/app/graph/nodes/parse.py:65-69`. `ground_to_request` only grounds fields when there is no
  free text.
* **Reproduction (granite, live):** "plan something fun for saturday", answered with city plus "3 hours, ₹1000",
  gave `interests: ["outdoor_walk", "shopping"]`. The plan was a bookstore, UB City mall and a heritage walk, "around
  shopping", which the user never asked for.
* **Fix:** keep only LLM interests whose stem appears in the free text or answers (the rules parser's interests are
  already grounded). Otherwise fall back to the "no interests given" assumption.

**D-P2-12: resuming an unknown or expired thread in JSON mode returns HTTP 200 with `status: "rejected"`.**

* **Where:** `backend/app/graph/runner.py:198-203` and `backend/app/api/routes.py:314-321`.
* **Effect:** §3 maps `rejected` to HTTP 400/413/429. JSON API clients can't branch on the status code. SSE has to
  stay 200, which is fine.
* **Fix:** in JSON mode, answer 404 or 410 with `{"status": "rejected", "detail": …}`. The frontend already handles
  both shapes.

**D-P2-13: protocol-error accounting is inconsistent, so the `tool_protocol_errors` metric isn't comparable.**

* **Counted:**
  * `UNKNOWN_ID` in `near_candidate_id` and `estimate_travel` (`backend/app/graph/tools.py:457,537`).
* **Not counted:**
  * `UNKNOWN_ID` inside `check_plan`/`submit_plan`, which become validator violations.
  * `CAP_REACHED` (`backend/app/graph/nodes/execute.py:162-170`).
* **Fix:** count only malformed calls, unknown tools and schema-invalid arguments, as §12 defines the metric.
  Report grounding errors (`UNKNOWN_ID`) as a separate counter.

**D-P2-14: heartbeat events reuse the previous event's `seq`; the first heartbeat has `seq: 0`.**

* **Where:** `backend/app/api/sse.py:66`.
* **Effect:** `seq` is therefore not unique per stream. The UI ignores heartbeats for dedupe, so nothing breaks
  today, but the contract should say so, or heartbeats should be sent as SSE comments (`: hb`).

**D-P2-15: rate-limit keying and window edges.**

* **Where:** `backend/app/api/security.py:214-230` and `backend/app/store/sqlite.py:86-89`.
* **IPv6:** keys use the full address, so one /64 holder can rotate freely. Key IPv6 by /64.
* **Window edges:** fixed windows allow twice the limit across a window boundary.
* **Local runs:** `uvicorn --proxy-headers` trusts loopback by default, so a spoofed `X-Forwarded-For` bypasses the
  limit when the client connects from 127.0.0.1 (verified on a local server: requests 6 and 7 passed). Production
  is safe because the Dockerfile deliberately avoids `--forwarded-allow-ips='*'` (`Dockerfile:90-94`). Keep it that
  way.

**D-P2-16: the SQLite checkpoint file is always `checkpoints.db` in the app database's directory.**

* **Where:** `backend/app/checkpoint.py:24,34`.
* **Effect:** two databases in one directory (`/tmp/a.db`, `/tmp/b.db`) share one checkpoint file.
* **Fix:** derive the name from the database file name (`a.checkpoints.db`).

**D-P2-17: the trace list is a single `aria-live="polite"` region with `aria-relevant="additions text"`.**

* **Where:** `frontend/src/components/TracePanel.tsx:148`.
* **Effect:** a screen reader announces all 25–50 steps one by one.
* **Fix:** announce milestones only, through a separate live region, and drop `aria-live` from the list.

**Domain observation (for the Phase 1 owners): idle gaps aren't flagged.**

* Granite's otherwise valid agent plan ran Leafy Lane Cafe 15:00–16:00, then an auto ride, then "Indie Acoustic
  Evening" at 17:30.
* That is about 54 minutes of waiting for a low-energy user, with no note.
* Consider a warning, or a "free time" pseudo-stop, when the wait before an event is 30 minutes or more.

---

## 2. Command results (backend `uv run …` from `backend/`, frontend `npm …` from `frontend/`)

| Command | Result |
|---|---|
| `uv run pytest` | **1442 passed, 8 deselected (live/ollama), 10 xfailed** (all 10 are this report's new D-P2 xfails). An intermediate run, while Phase 1 files were being edited, gave 1350 passed and 1 failed (`tests/unit/test_parser_rules.py::test_merge_llm_cannot_drop_constraints_and_request_fields_win`). That test passes at HEAD. |
| `uv run ruff check app tests` | All checks passed |
| `uv run mypy app` | Success: no issues in 67 source files. Before the Phase 1 commit there was 1 error, `app/cli.py:100 [no-redef]`, which is now fixed. |
| `uv run ruff check` / `mypy` on `tests/verification/test_phase2_api_graph.py` | clean |
| `RUN_OLLAMA=1 OLLAMA_MODEL=ollama:granite4:tiny-h uv run pytest -m ollama tests/graph/test_ollama_e2e.py` | 1 passed (156 s; this run fell back after a turn timeout, and the test only asserts a valid status) |
| `npm test` | 8 files, **73 passed** |
| `npm run typecheck` | exit 0 |
| `npm run lint` | exit 0 (`oxlint --deny-warnings src`; `npx oxlint e2e` is also clean) |
| `npm run build` | built (391.6 kB JS, 122 kB gzipped) |
| `npm run e2e` (new) | **11 passed** (one is the expected failure for D-P2-02), about 8 s plus the build |

Offline check of the Claude request payloads (no key; built with `ChatAnthropic._get_request_payload`):

* **Agent call:**
  * `max_tokens: 16000`
  * `output_config.effort: medium`
  * `fallbacks: "default"`
  * `betas: [server-side-fallback-2026-07-01]`
  * system block with `cache_control: ephemeral`
  * no `temperature` and no `tool_choice`
* **Parse call:** `output_config` holds both `effort: low` and the JSON-schema `format`, merged correctly.

---

## 3. Real-server end to end (uvicorn, fixture world, `PRIMARY_MODEL=fake:agent`)

Server: `APP_ENV=test PLACES_PROVIDER=fixture EVENTS_PROVIDER=fixture WEATHER_PROVIDER=fixture GEOCODER=fixture
FIXED_NOW=2026-11-05T04:30:00Z DATABASE_URL=sqlite:////tmp/... PRIMARY_MODEL=fake:agent`. Rate limits were raised
for flow testing, and a second instance with default limits was used for the rate-limit checks.

| Flow | Result |
|---|---|
| SSE plan (brief JSON) | 200 `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`, CSP present. 49 events. `run_started` first. Exactly one `result`, last. `node_completed finalize` immediately before it. `seq` strictly increasing. One `run_id`. Every `node_started` paired. `complete`/`agent` with `share_id`. Real tool calls (`search_places`×4, `search_events`, `estimate_travel`, `check_plan`, `submit_plan`), each with a reason. |
| Heartbeat | Seen live with Ollama (2–4 heartbeats per 40–48 s run, every 10 s of silence). Covered in-process by `test_heartbeat_is_sent_during_a_slow_run`. See D-P2-14 for the `seq` reuse. |
| JSON plan | `Accept: application/json` (or no `Accept`) gives a 200 JSON `PlanResult` with `share_id`. |
| Clarification, then resume (SSE) | "plan something fun for saturday" gives `needs_clarification`, 2 questions (city; budget and time) with quick replies, a `thread_id` and no share ID. Resume gives `complete`. `run_started.data.resumed = true`. The budget (₹1000) and time (180 min) answers are applied. |
| Clarification, then resume (JSON) | "bored. ideas?" plus quick-reply answers gives `complete` with `share_id`. |
| Resume of a terminated thread / an unknown thread | 200 `rejected` "This question has expired or was already answered" (SSE stream contract intact; see D-P2-12). Bad thread ID pattern gives 400. Empty answers give 400. |
| Concurrent resumes | 3 of 3 completed (D-P2-05). |
| Share link | `GET /api/plans/{id}` gives the same stops, no mood and no `thread_id`. Feedback `up` gives 204. Invalid rating gives 400. Unknown share gives 404 `{"detail":"Plan not found or expired"}`. 62 reads from one IP: 60 succeed, then 429 with `Retry-After`. |
| 400 | Negative budget, malformed JSON, extra field, NaN/Inf budget, 11 interests, a 41-character interest, array body, `text/plain` body: all give 400 `{"status":"rejected","detail":[field messages]}` with no echoed values. |
| 413 | 20 KB mood gives 413 `{"status":"rejected","detail":"Request too large"}` with security headers and `X-Request-ID`. |
| 429 | 6th plan from one IP in 10 minutes gives 429 `{"status":"rejected","detail":"Too many plans; try again in N seconds"}` with `Retry-After`. Also returned for SSE requests, as JSON. Resume is counted. 20 concurrent requests from one IP: exactly 5 succeed (atomic counter). |
| Health | 200 `{"status":"ok", …, "db":"ok"}`, `Cache-Control: no-store`. |
| Security headers | CSP (`default-src 'self'`, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY`, `X-Request-ID` on API, SPA, 404, 413 and 429 responses. |
| SPA serving | `/`, `/p/:id`, `/evals` and `/how-it-works` give `index.html` (`no-cache`). `/assets/*` misses and `/api/*` misses give JSON 404. Path traversal (`/../../etc/passwd`, `%2e%2e`) serves `index.html`, never a file. `/api/openapi.json` is exposed only when not in prod. |
| Prompt injection (mood, constraint, free text) | Guard flags it (`injection_flagged: true`) and planning continues. No system prompt, key or sentinel text in any body or trace. Budget enforced (injected free text with ₹500 gave worst case ₹385). |
| XSS (mood, city, interests, constraints, neighbourhood) | No raw `<` or `>` in any JSON or SSE body (escaped as `\u003c` and `\u003e`). The script-in-city request gets a clarification that quotes it, rendered literally in the browser. |
| Unicode | RTL override, zero-width and fullwidth characters, emoji and NUL bytes are normalised. Fullwidth "Ｂａｎｇａｌｏｒｅ" resolves. Kannada "ಬೆಂಗಳೂರು" gets a not-found clarification (fixture geocoder limit). |
| Huge arrays | 10×40-character interests and 10×120-character constraints plan fine. 11 items give 400. |
| Out-of-scope ("book me a table and pay") | Plans, with the assumption "I can't book, pay or contact anyone". |
| Other cases | London gives a GBP/INR question. Atlantisville gives a not-found question. Contradictory gives a "which should I keep" question. Budget ₹1 with vegetarian, wheelchair and sit-down meal gives `no_feasible_plan` with the cheapest workable budget. Mumbai plans from Mumbai fixtures. A 23:00 start gives one late-open stop. A past or far-future date falls back to the coming Saturday, with an assumption. |
| Client disconnect mid-stream | Read the first event, closed the connection. The run still finished, and the run row is `complete`/`agent`. A JSON client timeout doesn't cancel the run (shielded). |
| Rapid concurrency | 20 simultaneous plans (SSE and JSON mixed) all `complete` in 0.5 s. |
| Logs | JSON lines with `request_id`/`run_id`. No mood, free text, injection or XSS strings, and no keys. |

## 4. Browser E2E (Playwright, `frontend/e2e/`, real backend serving the built UI, fake model)

`npm run e2e` builds the UI, then starts uvicorn from `backend/` on the frozen world (`playwright.config.ts`). Set
`E2E_BASE_URL` to test an already running server instead.

| Test | Result |
|---|---|
| Prefilled brief plans end to end | Pass. Focus moves to "Your Saturday in Bengaluru". 3 timeline cards, each with clock times ("3:00 PM to 3:50 PM"), a ₹ cost or "free", a "Test data" badge and an https Google Maps link with `rel=noopener`. Legs show the "estimate" badge. Summary shows worst case of ₹2,000. Checks panel shown. Trace shows "N steps, M tool calls", `search_places`/`check_plan` code tags, "Agent's note" lines and "Plan passed the checks". No console errors or dialogs. |
| Brief's JSON via the form fields | Pass. Request body equals the brief's JSON, with no `free_text`. |
| Vague request, clarification, resume | Pass. Card focused. Submit disabled until both questions are answered. Quick replies, then POST `/api/plan/{thread}/resume`, then a plan of ₹1,000. Trace shows "Picked up again with your answer". |
| Typed answer, then expired thread | Pass. A typed "Mumbai" gives a Mumbai plan. Re-answering gives "expired or was already answered". |
| Relaxation (raise budget) | Pass. Resubmits with the patched budget and the same free text, and a plan appears. |
| Relaxation (drop constraint) | Expected failure: D-P2-02. |
| Share page | Pass. Same stop names, "Want one for yourself?", feedback "Yes" gives thanks. Unknown ID gives "This plan isn't available". |
| XSS renders literally | Pass. `<img src=x onerror=alert(1)> chill` shown as text. No `img[src=x]`. City with `<script>` shown inside the clarification legend. No dialog. |
| 429 UI | Pass, with a mocked response in the backend's exact 429 shape: "The planner is busy" plus the detail. |
| 390 px | Pass. No horizontal overflow on the planner, plan (trace expanded), clarification, infeasible, share, `/how-it-works`, `/evals` and the 404 share page. |
| Keyboard only | Pass. Skip link first. Tab to the textarea, type, Tab to "Plan my Saturday" (visible outline), Enter. Clarification heading focused. Tab plus Enter/Space on quick replies, Enter on "Continue planning". Plan heading focused. Reach "Copy plan" and feedback, then Shift+Tab back to "Show steps". |

Visual review (screenshots at 1280 px and 390 px) found D-P2-06, D-P2-07 and D-P2-09. The layout, type scale and
contrast otherwise look clean, and the mobile cards stack without clipping.

## 5. Ollama runs (real server, fixture world, API deadline 60 s)

`granite4:tiny-h`, run twice (before and after the Phase 1 commit, with identical outcomes):

* **Brief:** `complete`, agent mode, in 19.9 s and 23.1 s.
  * 5 turns, with one nudge after a text-only turn.
  * `search_places` ("Find a quiet vegetarian cafe for low-energy stop"), `search_events` ("Find a music-related
    event before sunset…"), `check_plan`, then `submit_plan`, which was valid on the first try.
  * Plan: Leafy Lane Cafe (60 min), then Indie Acoustic Evening (90 min). Worst case ₹784 of ₹2,000. All checks
    pass. 0 protocol errors. About 21k input and 1.1k output tokens. The prose has no numbers.
* **Vague request:** `needs_clarification` in 2.6 s, with the right 2 questions.
* **Resume:** `fallback` after a turn timeout (48 s). The parse invented interests (D-P2-11).
* **Injection in mood:** no leak. The model didn't submit, so `fallback` (14 s).
* **Free-text vegan:** `fallback` (40–48 s), after `check_plan`.

`gemma4:e4b`:

* **Brief:** `fallback`, timeout at 48 s.
* **Vague request:** clarified in 3.6 s.
* **Resume:** `fallback` at 48 s.
* **Injection:** `fallback` (no time left) at 48 s, with no leak.
* The model made sensible tool calls with real reasons ("Find a low-energy, vegetarian spot for a snack.") but
  never submitted inside the deadline.

Every Ollama stream met the SSE contract: one result, monotonic `seq`, and heartbeats on the slow runs. Every
displayed plan passed server-side validation. `qwen3.5:9b` wasn't run, to limit Ollama contention with the
concurrent evals agent.

## 6. Review notes: areas checked and found sound

* **Graph and deadline:**
  * Every model call is capped at `min(model timeout, remaining − 12 s)`.
  * `build_context` skips the agent when less than 15 s remain.
  * The runner hard-stops at deadline + 5 s.
  * Turn cap, one nudge then fallback, and 3 submissions (initial + 2 repairs) were verified with faults.
* **Event emission:** no double `result` in the error, pause or crash paths. The runner-side `closing()` only runs
  when the graph didn't emit.
* **Append-only history:** AIMessages are stored unchanged, so thinking blocks are preserved.
  * Every tool call, including malformed and duplicate `submit_plan` calls, gets exactly one ToolMessage.
  * Repairs come back as `submit_plan` error ToolMessages.
* **Tool caps:** enforced per run on executed calls inside a single turn too. Protocol-error calls don't reach
  providers.
* **Grounding:** `finalize` re-validates from the ledger. `UNKNOWN_ID` violations drop the invented IDs from trace
  events. With the `submit_plan_unknown_ids_twice` fault, the displayed fallback only contains fixture IDs.
* **Prompt boundary:**
  * System prompts are static.
  * User text and tool data travel as JSON in Human/Tool messages.
  * Event descriptions never reach the model, and names are clipped to 120 characters.
* **Secrets:** a sentinel in the Google and Tavily key settings never appeared in responses or logs.
  `_describe` doesn't echo ValidationError input.
* **Storage redaction:** share records drop mood, soft preferences, constraint `source_text` and `thread_id`.
  * Google display fields are removed and names tokenised. This was verified by code review and the owners'
    `tests/store/test_redact.py`, not live.
  * `maps_route_url` uses coordinates only (30-day TTL).
* **Frontend contract:** the TS types match the real JSON. The only differences are diet record keys and
  eval-summary fields. The `<`/`>`/`&` escapes decode transparently. Error shapes (list or string `detail`, 404,
  413, 429 with `Retry-After`, 500 with `request_id`) are all handled.

## 7. What could not be verified

* **Live Claude calls** (`anthropic:claude-opus-5-5`): no key. Only the request payloads were checked offline (§2).
  Not checked: whether the API accepts `output_config`/`fallbacks`/`betas`, refusal handling against the real API,
  `cache_read_input_tokens` on repeat calls, and cost figures.
* **Gemini and Grok:** pending in the registry (no pinned IDs and no keys).
* **Google Places, Tavily and Open-Meteo live:** no keys, and everything ran on the frozen world. Share-link
  re-fetch of Google display fields was not exercised live.
* **Postgres store and `AsyncPostgresSaver`:** no Postgres running. Only SQLite and in-memory were exercised.
* **Render or Docker deploy and proxy behaviour:** not run. Not verified: the right-most `X-Forwarded-For` logic
  behind Render's proxy, and the SIGTERM drain.
* **`qwen3.5:9b`:** not run (see §5).
* **Colour contrast and full screen-reader passes:** spot-checked visually only, with no axe scan. Only
  `@playwright/test` was allowed as a dependency.

## 8. Files added by this verification

* `backend/tests/verification/test_phase2_api_graph.py`: 7 passing contract tests and 10 strict xfails
  (D-P2-01 ×4, 02, 03, 04, 05, 06, 07).
* `frontend/e2e/playwright.config.ts`, `frontend/e2e/helpers.ts` and `frontend/e2e/planner.spec.ts`: 11 browser
  tests.
* `frontend/package.json`: `@playwright/test` devDependency and the `e2e` script (`package-lock.json` updated by
  npm). Run `npx playwright install chromium` once before the first `npm run e2e`.
* `docs/verification/PHASE2.md`: this report.
