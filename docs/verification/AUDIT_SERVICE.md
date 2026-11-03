# Independent service-layer audit

Date: 2026-11-03. Scope: `backend/app/graph/**`, `backend/app/llm/**`, `backend/app/api/**`, `backend/app/main.py`,
`backend/app/store/**`, `backend/app/checkpoint.py`, `backend/app/observability.py`, `backend/app/trace.py`,
`backend/app/config.py`, `backend/app/deps.py`, `Dockerfile`, `render.yaml`, `.github/workflows/ci.yml`, `Makefile`,
`.env.example`, checked against [HLD.md](../HLD.md) and [LLD.md](../LLD.md).

The auditor did not write this code and changed no existing file. The "underfilled plan" defect, which was being
fixed while this audit ran, is excluded. Every finding below has a reproduction except AUD-S45, which comes from
reading the Postgres code (no Postgres server was available). Each reproduction is a test marked
`@pytest.mark.xfail(strict=True, reason="AUD-S<n>: ...")`: it fails today, and it becomes a strict XPASS failure once
the bug is fixed, which is the signal to remove the marker.

Reproductions:

| File | IDs |
|---|---|
| `backend/tests/audit/test_audit_service_graph.py` | AUD-S1 to AUD-S9 |
| `backend/tests/audit/test_audit_service_privacy.py` | AUD-S10 |
| `backend/tests/audit/test_audit_service_api.py` | AUD-S20 to AUD-S24 |
| `backend/tests/audit/test_audit_service_store.py` | AUD-S40 to AUD-S44 |

How the tests run: offline only. They use the scripted fake models (`fake:agent`, `fake:secondary`,
`fake:primary_503`, `settings.fault`), the frozen-world fixture providers, synthetic slow or failing provider
wrappers, in-memory SQLite and checkpointers, and a fake psycopg pool that adapts parameters with psycopg's real
`Transformer`. No real model or data API was called. The server on :8000 was not touched. A separate uvicorn on :8765
(fixture providers, `fake:agent`, key variables empty, no `.env`) was used for live checks, then stopped and its temp DB
deleted.

Severity scale:

* **P0**: security, data loss, or a wrong result shown.
* **P1**: realistic wrong behaviour.
* **P2**: edge case or cosmetic.

## 1. Bug list

| ID | Sev | Finding | Root cause | Reproduction |
|---|---|---|---|---|
| AUD-S1 | P1 | The fallback planner isn't time-bounded: when the agent times out, slow providers can push the run past deadline + 5 s, and the user gets "Planning took too long" with no plan | `graph/nodes/fallback.py:83-87`; `graph/nodes/common.py:24` (12 s reserve); `graph/runner.py:267-274` | `test_aud_s1_fallback_after_agent_timeout_still_returns_a_plan` |
| AUD-S2 | P1 | A failed events provider is queried again in the same run (`search_events` and `search_places include_events`) | `graph/tools.py:678-692` (no `env.events_failed` check), `:660-661` | `test_aud_s2_failed_events_provider_is_not_retried` |
| AUD-S40 | P1 | Share records store the Google names of omitted places (`plan.omitted[].name`, and prose that mentions them) | `store/redact.py:84-118`; `graph/nodes/finalize.py:127`; `domain/fallback.py:813` | `test_aud_s40_*` (3 tests) |
| AUD-S3 | P2 | `include_events` bypasses the `search_events` cap: 5 event searches in one run against a cap of 2 | `graph/tools.py:660-661` | `test_aud_s3_event_searches_respect_the_per_run_cap` |
| AUD-S4 | P2 | `fallback_planner` re-queries a live places provider that the run already marked unavailable | `graph/nodes/fallback.py:83`, `pipeline.py:112` | `test_aud_s4_fallback_does_not_requery_a_failed_live_provider` |
| AUD-S5 | P2 | Clarification answers skip `guard_input`: bidi and zero-width characters reach preferences, and a clamped time answer gets no visible assumption | `graph/nodes/clarify.py:99-108` | `test_aud_s5_*` (2 tests) |
| AUD-S6 | P2 | An unknown or pending `FALLBACK_MODEL` disables a working primary for every plan | `llm/registry.py:465` / `:480-481` (eager `get_spec`) | `test_aud_s6_bad_fallback_model_does_not_disable_the_primary` |
| AUD-S7 | P2 | When the primary returns 503 and the fallback returns 429, the run reports "unavailable", not the rate limit | `graph/nodes/agent.py:127-135`; LangChain `RunnableWithFallbacks` re-raises the first error | `test_aud_s7_rate_limit_that_ends_the_chain_is_reported` |
| AUD-S8 | P2 | `llm_calls` does not count model calls cancelled by a timeout, and `_run_models` leaks an entry for each | `llm/usage.py:63-86` | `test_aud_s8_*` (2 tests) |
| AUD-S9 | P2 | At the hard deadline, `node_started` and `tool_called` trace events are left with no matching end event | `graph/runner.py:288-296`; `trace.py:80` (only catches `Exception`) | `test_aud_s9_hard_deadline_trace_has_no_dangling_starts` |
| AUD-S10 | P2 | The access log records the raw resume path, so a failed resume attempt leaves a working resume token for the paused request in the logs | `api/security.py:92` | `test_aud_s10_access_log_never_holds_a_usable_resume_token` |
| AUD-S20 | P2 | 400 responses repeat what the user sent: unknown field names, dict keys and the `relaxed_constraints` value | `main.py:35-44`; `domain/schemas.py:103` | `test_validation_errors_never_echo_user_input` |
| AUD-S21 | P2 | `GET /%00` causes an unhandled 500, logged at ERROR and sent to Sentry, with no rate limit | `main.py:158` | `test_nul_byte_in_path_is_not_a_server_error` |
| AUD-S22 | P2 | `HEAD /api/health` returns 404 (SPA mounted) or 405 (no SPA), not 200 | `api/routes.py:461` | `test_head_health_answers_like_get` |
| AUD-S23 | P2 | The CI `model-evals` job lacks `CEREBRAS_API_KEY`, `GROQ_API_KEY` and `JUDGE_MODEL`; its default models are the baseline and an unconfigured Claude | `.github/workflows/ci.yml:11`, `:240-244` | `test_ci_model_evals_job_gets_the_production_model_keys` |
| AUD-S24 | P2 | A rejected concurrent resume deletes the `paused_threads` row of a thread that another call is still resuming | `api/routes.py:229-237` | `test_rejected_concurrent_resume_keeps_the_paused_thread_record` |
| AUD-S41 | P2 | `app.db`, `app.sqlite3` and `app` all share `app.checkpoints.db` | `checkpoint.py:36-37` | `test_aud_s41_checkpoint_files_are_distinct_per_app_database` |
| AUD-S42 | P2 | A finished run whose checkpoint delete failed keeps the raw request forever, because no sweep covers it | `graph/runner.py:81-87`; `store/retention.py:38-41` | `test_aud_s42_orphaned_run_checkpoint_is_eventually_swept` |
| AUD-S43 | P2 | NUL characters are accepted; SQLite stores them but Postgres raises `DataError`: feedback returns 500, and share saves fail on Postgres only | `api/routes.py:67-71`, `:405`; `store/postgres.py:153`, `:161-163` | `test_aud_s43_feedback_with_nul_behaves_the_same_on_both_stores` |
| AUD-S44 | P2 | A `~` in a SQLite `DATABASE_URL` breaks startup: `mkdir` expands it but `connect` does not | `store/sqlite.py:163-164` | `test_aud_s44_tilde_sqlite_url_opens_the_expanded_path` |
| AUD-S45 | P2 | Postgres JSONB reorders the keys of the published eval summary, so `/evals` tables come out in a different order (code reading) | `store/postgres.py:241-249`; `frontend/src/pages/EvalsLeaderboard.tsx:110-114` | none (no Postgres) |

Totals: 0 P0, 3 P1, 18 P2. That is 21 findings: 20 reproduced by 24 xfail tests, and 1 from code reading.

## 2. Findings in detail

### AUD-S1 (P1): a timed-out agent plus slow providers gives "Planning took too long" with no plan

**What happens:** `plan_agent` lets a model call run until 12 s before the deadline (`DEADLINE_RESERVE_S`). The
runner hard-stops at deadline + 5 s. That leaves the fallback 17 s at most. When the agent produced no usable ledger,
`fallback_planner` first runs the default place searches and then the event search, with no time limit of its own.
The documented per-search budgets are 8 s for places and 15 s for a whole event search, 23 s together, which is
more than the reserve.

**Reproduction:** `fault=llm_timeout`, `run_deadline_s=16`, places taking 3 s, events taking 15 s. The result is
`rejected` "Planning took too long" after 21.0 s. The safety net exists to return a rule-based plan, and here it
returns nothing.

**Fix:** pass the remaining time into the fallback's searches. For example, skip or time-limit
`search_events` to `rc.remaining() + HARD_GRACE_S - margin` and wrap `search_places` in `asyncio.wait_for`. Or size
the reserve from the provider budgets.

### AUD-S2 (P1): a failed events provider is retried within the run

**What happens:** `run_search_events` sets `env.events_failed` but never reads it.

**Reproduction:** turn 1 calls `search_events` and the provider fails. Turn 2 calls `search_places` with
`include_events` and then `search_events`. The provider is called 3 times. HLD §11 says "an events failure marks
`events_failed` and is not retried".

**Impact:** each retry of a down Tavily costs up to 8-15 s of the 60 s run, which feeds AUD-S1.

**Fix:** at the top of `run_search_events`, if `env.events_failed`, return the `EVENTS_UNAVAILABLE` outcome without
calling the provider.

### AUD-S40 (P1): Google display names of omitted places are stored in share records

**What happens:** `with_omission_names` (finalize) and the rule-based planner's `_omissions` copy the Google name into
`Omission.name`. `for_storage` only redacts and tokenises the stop and Plan B candidates. Prose that mentions omitted
places is not tokenised either.

**Reproduction:** run the real graph with fixture places relabelled as `gp:` / `google_places`. The stored record
contains "The Malabar Table" and "Chowdiah Memorial Hall" in both agent and baseline mode. This contradicts HLD §9
and §16 ("Google display fields are never stored"), and the name is kept for the 30-day TTL.

**Fix:** in `for_storage`, blank `Omission.name` for Google refs and add those names to the tokenisation map. In
`rehydrate`, restore them by fetch, falling back to "this place".

### AUD-S3 (P2): `include_events` bypasses the `search_events` cap

**What happens:** the event search inside `run_search_places` is not counted in `tool_exec["search_events"]`.

**Reproduction:** three `search_places` calls with `include_events`, plus three `search_events` calls, run 5 event
searches. The cap is 2 (`TOOL_CAPS`).

**Fix:** count it against the cap, and skip it (with a note in the result) once the cap is reached.

### AUD-S4 (P2): the fallback re-queries a failed live places provider

**What happens:** `fallback_planner` calls `pipeline.search_places`, which always starts with `deps.places` and
ignores the run's sticky `places_mode`.

**Reproduction:** Mumbai (not covered by the snapshot) with a failing live provider: the provider is called 5 times
in the run, after it had already been marked unavailable. With a provider that times out, that is another 8 s spent
in the fallback (see AUD-S1).

**Fix:** pass `places_mode` to the fallback. Use the snapshot directly when the mode is `snapshot`, and skip the
search when it is `unavailable`.

### AUD-S5 (P2): clarification answers bypass `guard_input`

**What happens:** `ask_clarification` folds the raw answers into the request (`apply_answers`), then routes to
`parse_preferences`. `guard_input` never runs on them.

**Reproduction:**
* City answer `"Bangalore​"` and mood answer `"chill ‮evil"` come through to `result.preferences` unchanged,
  zero-width and bidi characters included. The same text in the original request is stripped.
* The answer "20 hours" silently gives a 720-minute plan without the "Capped at 12 hours" assumption that the same
  text gets in the original request.
* The store fork also confirmed that a NUL in a `neighbourhood` answer reaches `preferences` (see AUD-S43).
* Injection screening does not run on answers either.

**Fix:** run `normalize_request` and `screen` on the request after `apply_answers` (or route `ask_clarification` back
through `guard_input`), keeping the clamp assumptions.

### AUD-S6 (P2): a bad `FALLBACK_MODEL` disables a working primary

**What happens:** `build_agent_runnable` and `build_structured_runnable` call `get_spec` for every model in the chain
before invoking anything.

**Reproduction:** with `FALLBACK_MODEL=fake:typo` (or `xai:<pending>`), every parse and every agent turn raises
`RegistryError`. The primary `fake:agent` never runs. The user sees "The AI planner isn't configured" on every plan,
and nothing at startup or in `/api/health` reports the misconfiguration.

**Fix:** validate `PRIMARY_MODEL` and `FALLBACK_MODEL` at startup (fail fast or log an ERROR), and/or drop an
unbuildable fallback from the chain with a warning.

### AUD-S7 (P2): the rate-limit classification uses the primary's error

**What happens:** `RunnableWithFallbacks.ainvoke` re-raises the *first* error. `plan_agent` classifies that error.

**Reproduction:** `fake:primary_503` as primary, and a fallback that returns HTTP 429: the run reports "The AI
planner was unavailable". HLD §8 says that when the error that ends the chain is a rate limit, the run reports the
rate-limit message. The eval category `provider_rate_limited` is mis-assigned the same way.

**Fix:** keep the last error. For example, wrap each chain member to record its exception in a per-call list, or
catch the error in a custom fallback loop, and classify that last error.

### AUD-S8 (P2): `llm_calls` misses timed-out calls

**What happens:** a model call cancelled by `asyncio.wait_for` (the turn timeout, or the primary's share in
`with_timeout`) reaches neither `on_llm_end` nor `on_llm_error`.

**Reproduction:**
* The `model_timeout` scenario reports `usage.llm_calls == 0` for one real agent request.
* A primary cut at its time share followed by a fallback answer reports 1 call instead of 2, and leaves one stale
  `_run_models` entry.

The eval call-budget metrics under-count accordingly.

**Fix:** count at `on_chat_model_start`, and price at end or error. Or count the entries left in `_run_models` when
the node finishes.

### AUD-S9 (P2): the hard deadline leaves unmatched trace events

**What happens:** when the runner cancels the graph task, the cancelled node's `node_completed` (from
`Tracer.node`'s `finally`) is written to a stream nobody reads anymore. `Tracer.tool` only handles `Exception`,
so a `CancelledError` emits no `tool_failed`.

**Reproduction:** a hang inside `fallback_planner`. The trace ends with `node_started fallback_planner` and
`tool_called search_events`, neither completed, before the synthetic finalize. That breaks the
one-start-one-end contract the existing trace-integrity helper asserts.

**Fix:** in the runner's error path, emit closing events for the open nodes and tools (it already knows them from the
events it forwarded). Or catch `BaseException` in `Tracer.tool` and re-raise.

### AUD-S10 (P2, privacy): resume tokens in the access log

**What happens:** `SecurityHeadersMiddleware` logs the raw path. For `/api/plan/{thread_id}/resume`, the thread ID
is the only credential for a paused checkpoint, which holds the raw request (free text, mood) for up to 24 h.

**Reproduction:** a resume attempt rejected with 400 or 429 is logged while the thread is still paused. Replaying the
logged ID resumes the plan and returns the user's private mood text.

**Fix:** log the route template (`/api/plan/{thread_id}/resume`), or redact path IDs, in the access line.

### AUD-S20 (P2): 400 responses echo user input

**What happens:** `validation_messages` copies pydantic's `loc` and `msg` unchanged.

**Reproduction:**
* An unknown body field returns `"zzusersecretzz: Extra inputs are not permitted"`.
* A bad resume answer key appears inside the location.
* `relaxed_constraints` returns `"unknown constraint key: diet:zzusersecretzz"` (from `schemas.py:103`).

The output is HTML-escaped, so this is not XSS, but it contradicts HLD §10 ("field messages that never echo values").

**Fix:**
* For `extra_forbidden` and dict-key errors, report the parent field only.
* Give `value_error` fixed messages.
* Remove the `{key}` interpolation in `schemas.py:103`.

### AUD-S21 (P2): `GET /%00` gives a 500

**What happens:** `(dist / full_path).resolve()` raises `ValueError: embedded null character`. ErrorShield logs
"unhandled error" at ERROR and calls Sentry.

**Reproduction:** `/%00`, `/assets/x%00.js` and `/plans/a%00b` all return 500. This was also confirmed on the live
:8765 server. Any client can produce ERROR logs and Sentry events without limit.

**Fix:** treat a NUL in the path as 404, or catch `ValueError`/`OSError` around `resolve()`.

### AUD-S22 (P2): `HEAD /api/health` is not served

**What happens:** `/health` is registered for GET only.

**Reproduction:** with the SPA mounted, the catch-all GET/HEAD route returns 404 for the API path. Without the SPA,
the response is 405. A monitor that probes with HEAD sees the service as down. Docker and Render use GET and are
not affected.

**Fix:** `@router.api_route("/health", methods=["GET", "HEAD"])`.

### AUD-S23 (P2, CI config): the `model-evals` job can't run the production models

**What happens:** the job's environment passes the Anthropic, Google, xAI and LangSmith keys. It does not pass the
keys for the deployed primary and fallback (Cerebras, Groq), and `JUDGE_MODEL` is unset. Its default `models` input
is `baseline:deterministic,anthropic:claude-opus-5-5`, and that Claude model is "not configured".

**Fix:**
* Add the `CEREBRAS_API_KEY` and `GROQ_API_KEY` secrets, and optionally `JUDGE_MODEL`.
* Change the default to `baseline:deterministic,cerebras:gpt-oss-120b,groq:openai/gpt-oss-120b`.

### AUD-S24 (P2, retention): a rejected concurrent resume forgets a thread that is still in flight

**What happens:** `_track_paused_thread` calls `forget_paused_thread` for every resume result that isn't
`needs_clarification`. That includes the runner's `ALREADY_RESUMING` rejection ("another call owns this thread").

**Reproduction:** while resume #1 is still running, resume #2 is rejected and the `paused_threads` row is gone. If
the process dies during resume #1, the checkpoint (raw request) is never swept.

**Fix:** forget the row only for results that end the thread. Skip `ALREADY_RESUMING`, or have the runner expose an
"owned" flag the API can check.

### AUD-S41 (P2): checkpoint file names collide

**What happens:** `checkpoint_path_for` drops the suffix, so `sqlite:////srv/data/app.db`, `.../app.sqlite3` and
`.../app` all map to `/srv/data/app.checkpoints.db`. The module docstring promises that two app databases in one
directory never share checkpoints.

**Fix:** keep the full file name, for example `app.db.checkpoints.db`.

### AUD-S42 (P2, privacy): orphaned checkpoints are never swept

**What happens:** `delete_thread` only logs a cleanup failure. The sweeper only knows about threads recorded as
paused, so nothing retries the others. A process crash mid-run has the same effect.

**Reproduction:** the checkpointer's first delete raises `OSError`. The checkpoint still holds the user's free text
after a sweep 30 days later, even though the delete then works.

**Fix:** record every thread at run start and sweep anything older than the run deadline that isn't freshly paused,
or sweep checkpoints by age.

### AUD-S43 (P2): NUL bytes behave differently on SQLite and Postgres

**What happens:** neither the feedback `reason` nor resume answers filter NUL. Postgres rejects NUL in `text` and in
JSONB.

**Reproduction:** with SQLite, a feedback reason containing NUL returns 204. Through `PostgresStore` (fake pool,
real psycopg adaptation) the same request returns 500.

**Postgres only (code reading):** a NUL that reaches a plan through a resume answer (see AUD-S5) makes
`save_plan`'s JSONB insert fail. The failure is logged and that plan gets no share link.

**Fix:** strip control characters, NUL included, at the API boundary (feedback, resume answers) and in `guard_input`.

### AUD-S44 (P2): a `~` in a SQLite `DATABASE_URL` breaks startup

**What happens:** `init` runs `mkdir` on the expanded path but `sqlite3.connect` on the literal `~/...` path, which
fails with `unable to open database file`. The checkpointer uses the expanded path, so the two disagree.

**Fix:** set `self.path = str(Path(path).expanduser())` in `__init__` for file databases.

### AUD-S45 (P2, by code reading): `/evals` table order changes on Postgres

**What happens:** `save_eval_summary` stores the summary as JSONB, which does not keep object key order (keys are
sorted by length, then bytes). SQLite and the bundled `summary.json` keep insertion order. The leaderboard builds its
"By category" rows and columns from key order, so after `evals.report --publish` the production page would list the
ollama rows before `baseline:deterministic`.

**Fix:** store the summary as `json` or `text`, or sort explicitly in the UI.

## 3. Checked and found correct

**Graph routing:**
* Every node's routes under:
  * exceptions in tools, which return `TOOL_ERROR` to the model;
  * refusals (primary and chain), timeouts, provider errors;
  * the client-side `RateLimitWaitError` (classified as a 429);
  * unknown tools, malformed arguments, schema-invalid arguments, unknown IDs, cap reached, a duplicate
    `submit_plan`;
  * the settings turn cap and per-model turn caps;
  * a nudge followed by a second text-only turn, then checked-draft salvage;
  * the repairs-exhausted path.
* The soft deadline (`build_context` skipping the agent under 15 s, nodes under 12 s) and the hard deadline give
  `rejected` "Planning took too long".
* The geocoder `RetryPolicy` retries exactly once on a retryable `ProviderError`.

**Runner:**
* Exactly one `result` event on every path: the graph result, the clarification pause, rejected resumes, a crash and
  the hard deadline. A duplicate result is dropped by both the runner and `produce_events`.
* Checkpoints are deleted at terminal statuses and kept while paused. A resume of an unknown, finished or running
  thread gets `EXPIRED` and doesn't touch the thread.
* A second or concurrent resume gets `ALREADY_RESUMING` or `EXPIRED`, and the lock is released and removed.
* Messages are append-only, and every tool call gets exactly one ToolMessage (`submit_plan`'s comes from
  `validate_plan`).
* A resume reports only its own usage (`usage_base`). `rate_limit_wait_ms` is reported once and removed from `usage`.
* `protocol_errors` equals the sum of the kinds.

**Rate limiter:**
* The sliding-window maths was property-checked on 300 random arrival sequences: no half-open window of `window_s`
  ever holds more than `requests` bookings.
* Bookings are atomic under the lock, so concurrent async callers get distinct slots.
* A wait that doesn't fit the budget raises `RateLimitWaitError` (status 429) at once and books nothing.
* Nested budgets only shorten. `with_timeout` gives the primary its share, and the fallback then runs under the
  outer budget.
* `time.monotonic` is used throughout.
* No unbounded growth was found: the limiter dict is per model, the model cache is capped at 64, the compiled-graph
  cache at 8, and resume locks are removed.

**Registry:**
* The provider kwargs for Anthropic, Ollama (named and ad hoc), Google, Cerebras (`max_retries` 0 is kept), Groq and
  xAI match LLD §6.2.
* The structured-output paths: a dict schema for OpenAI-compatible providers (validated back into the class),
  `function_calling` for the qwen and gemma models, text-JSON recovery, refusal detection.
* The model cache key includes the fault, role, purpose and Ollama URL.
* `fake:primary_503` exercises `with_fallbacks`, and the answering model is tagged and sticks for the rest of the run.

**API (API fork):**
* SSE framing: `data: <json>` frames with no raw newline, and `: keep-alive` comments during slow runs (7 seen on a
  16 s run).
* A client disconnect doesn't stop the run being persisted. JSON mode awaits a shielded task.
* Error shapes 400, 404, 413, 429 (with `Retry-After`) and the generic 500 with a request ID.
* Security headers on 200, 404, 405, 413, 500 and SPA responses. No CORS headers. OpenAPI is hidden in prod.
* SPA path traversal is blocked (`..`, `%2e%2e`, absolute paths, symlinks). Chunked bodies over the cap get 413.
* Right-most `X-Forwarded-For` hop (across repeated headers too), IPv6 keyed by /64, IPv4-mapped IPv6 keyed by its
  IPv4 address.
* Resume returns 404 in JSON when expired. Health returns 503 on a DB failure and reports places status.
  `newest_summary` handles time zones.

**Storage (store fork):**
* The two-window estimate and retry-after were checked on 200,000 random cases.
* 200 concurrent `hit_limits` calls admit exactly the cap, denied hits are not counted, and the daily cap resets at
  UTC midnight.
* Expiry boundaries are right, `close()` waits for in-flight queries, and paused threads older than 24 h are deleted
  with retry.
* Postgres code (by reading) takes locks in a consistent order, rolls back correctly on an autocommit pool, and its
  day keys, `get_run` types and row counts match SQLite.

**Share links:**
* Mood, soft preferences, `source_text` and `thread_id` are dropped.
* Stop and Plan B names are tokenised and restored through a mocked Google provider, including the failure and
  timeout paths.
* An injected `[[place:...]]` token can only become "this place" or the real name.

**Privacy and observability:**
* Logs carry IDs, counts and codes; Google and Tavily keys travel in headers; secrets are `SecretStr`.
* Sentry runs without PII, without stack locals and without request bodies.
* LangSmith hides inputs and outputs in prod.
* System prompts are static, and user text only reaches the model as JSON data in Human messages.

**Deploy config:**
* Every `config.py` field is in `.env.example`, and every `render.yaml` and CI `-e` variable maps to a field.
* The Dockerfile has one worker, `--proxy-headers`, a HEALTHCHECK that fails on 503, uid 1000, and a writable
  `/app/var` only.
* The CI smoke expectations match the current API.

## 4. Not verified

* **Postgres:** the Postgres store and checkpointer were checked by reading the code only.
* **Live providers:** Google Places, Tavily, Open-Meteo, Cerebras and Groq were not called.
* **Groq parameters:** whether Groq accepts `reasoning_format: hidden` together with `reasoning_effort` for
  `openai/gpt-oss-120b` could not be checked offline. One recorded Groq eval run completed in agent mode.
* **SSE through the frontend:** behaviour behind Render's proxy and in the frontend parser was not exercised.

## 5. Commands and results

From `backend/`:

* `uv run pytest tests/audit/test_audit_service_graph.py tests/audit/test_audit_service_privacy.py
  tests/audit/test_audit_service_api.py tests/audit/test_audit_service_store.py -p no:cacheprovider -rxX`
  gives `24 xfailed`.
* With `--runxfail`, every test fails at its final assertion: the observed values above, not a setup error.
* `uv run ruff check tests/audit/test_audit_service_*.py` gives `All checks passed!`.
* `uv run mypy tests/audit/test_audit_service_graph.py` gives `Success: no issues found in 1 source file`.
* `uv run pytest -q` passes with no failures (the audit tests are xfail).
