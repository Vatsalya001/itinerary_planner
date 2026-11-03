# Performance release — 3 November 2026

## Evidence

Production run `r_5975098c81d7` took 43.72 seconds. Its first submission exceeded INR 1,500 (computed INR 1,768); a second submission passed. The repair turn took 32.17 seconds. Render logs for that run show the Groq SDK announcing a 30-second retry delay. The log did not expose the original HTTP status, so free-tier throttling is plausible but not proven for that specific retry. The remaining wall-clock time was about 13.72 seconds; subtracting the wait is not a prediction of future live latency.

## Changes

- Disable Groq SDK automatic retries for parsing, planning and judging. A provider Retry-After cannot silently consume 30 seconds of the app's 60-second deadline. Existing rule-based/parser recovery and validated deterministic planner remain available. This deliberately favours prompt recovery over waiting out long quotas; it does not increase provider quota or guarantee agent completion on a free account.
- Attach safe provider HTTP status and numeric Retry-After metadata to logs and trace events, without exception response bodies/credentials. Per-agent-turn trace data now includes token counts and local limiter waits. Actual mocked Groq 429 testing confirms one HTTP attempt and no SDK retry sleep.
- Add an optional `validated_option` to successful place search results: the existing deterministic planner builds a candidate-ID/duration option, validates it, and includes its computed cost/time and covered/uncovered interests. The model can select/adapt it and must still call submit_plan; final validation is unchanged. Option generation has a 0.5-second async wait cap, runs off the event loop, and is skipped on failure. Its bounded worker may finish after a timeout; it makes no model/network/database calls.
- Compact model-facing candidates by omitting redundant source/rating-count fields and unknown diet entries; missing evidence is explicitly unknown. Effort and all important cost/hours/duration/access evidence remain available. Full records are unchanged in tool artifacts and the candidate ledger. Already-returned records are referenced by ID instead of being repeated in later tool results.
- Recognize the evidence instruction “Don't assume unknown facilities are accessible” without a spurious clarification; real and mixed unsupported access requirements still block as before.

## Verification

- Full offline backend suite: 1,847 passed, 11 live/Ollama cases deselected.
- Browser suite: 21 passed; builds production frontend and uses the offline fixture server.
- Offline workflow/fault eval: 32/32.
- Deterministic model-suite baseline: 26/26.
- Ruff and mypy passed.
- After preserving the effort field in the compact payload, 60 targeted provider/option/trace/tool tests passed again.
- No hosted model calls were made. No new real-model quality or live latency benchmark is claimed.

## Acceptance after deploy

Check `/api/health` for the release commit. On a fresh live request, search tool trace data can report `validated_option: true`, and per-agent-turn data reports tokens. On a quota error, the trace should report provider status/Retry-After and recover without the previous hidden SDK wait. Continue to distinguish agent completion from a valid deterministic fallback. A free Groq token allocation can still be insufficient; reduced latency does not establish production capacity.
