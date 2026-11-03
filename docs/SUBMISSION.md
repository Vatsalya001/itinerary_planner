# Submission checklist

## Current submission status — 3 November 2026

- Public app: https://itinerary-planner-r54t.onrender.com/
- Source: https://github.com/Vatsalya001/itinerary_planner
- Code release CI: https://github.com/Vatsalya001/itinerary_planner/actions/runs/41679470319 (passed for `48a78a2`; final backup-summary follow-up is verified separately).
- Anonymous Mumbai UI and share-link smoke: [recorded evidence](verification/SUBMISSION_SMOKE_2026-11-03.md). The earlier Groq run verified labelled fallback; the earlier Cerebras run verified orchestration/sharing. Both predate the practical-guardrail corrections.
- Fresh Mumbai and London acceptance runs completed in Cerebras agent mode without degradation; both anonymous share pages retained the correct caveats. See the [current guardrails release](verification/PRACTICAL_GUARDRAILS_2026-11-03.md) for exact observations, regression results and remaining limits. The older admission/transport defects and historical runs are retained as earlier evidence, not current practical-validity claims.
- Keep Cerebras credits/quota available. The application retains its trial-safe 5-call/minute limiter; allow at least a minute between demo runs. Billing tier was not inspected.
- Local interview handbook is private and intentionally uncommitted.

## Latest follow-up

The [requested-window policy](verification/WINDOW_POLICY_2026-11-03.md) now targets 85% utilization with an 80–100% practical range, safely fits the final flexible venue and defaults to medium energy when no mood is supplied.

The Mumbai underfill, Delhi tour-listing/turn-budget and Gurugram preference/export findings are addressed in the [plan-quality release](verification/PLAN_QUALITY_2026-11-03.md). Fresh Mumbai and Delhi requests completed in agent mode, while Gurugram used a labelled partial backup. Missing interests remain disclosed; these runs do not demonstrate full-interest coverage or universal reliability. Earlier smoke results remain historical observations.

## Required deliverables

- Public hosted UI URL, tested without the author's login.
- Source repository: https://github.com/Vatsalya001/itinerary_planner
- README with local setup, architecture, tools, provenance, limitations and AI-use disclosure.
- Fresh eval report at `docs/verification/RELEASE_EVAL_REPORT.md`.
- Optional demonstration script at `docs/DEMO_SCRIPT.md`.

The original assignment allows mock data and requires three tools/functions, a realistic personalized plan, explanations, graceful failure and a simple execution trace. It specifies a two-hour limit. This repository contains additional work; do not claim it was built within that limit unless that is true of your own build process.

## Minimal Render demo configuration

Create a Docker web service from the source repository. Review the hosting plan and any charges before provisioning. The repository's production blueprint additionally requires Postgres; the minimal disposable demo below does not.

```dotenv
APP_ENV=prod
PRIMARY_MODEL=cerebras:gpt-oss-120b
CEREBRAS_API_KEY=<set privately in Render>
FALLBACK_MODEL=
DATABASE_URL=sqlite:////tmp/saturday/app.db
FRONTEND_DIST=/app/frontend/dist
GEOCODER=open_meteo
WEATHER_PROVIDER=open_meteo
PLACES_PROVIDER=osm_snapshot
EVENTS_PROVIDER=none
TRUST_PROXY_HEADERS=true
RUN_DEADLINE_S=60
DAILY_PLAN_CAP=200
TRACING_ENABLED=false
```

The live service selects Cerebras through Render environment variables, overriding the repository Blueprint's Groq example. Check Cerebras account credits/limits before evaluator traffic; the successful smoke does not establish sustained capacity.

Optional: add another registered model with its provider key; add Google Places and event provider keys for wider data coverage. For the snapshot demo, tell reviewers that Bangalore is the supported city. Never label this snapshot as a live Places lookup.

Use `/api/health` as the health check. The image accepts the host-provided `PORT`. SQLite data in `/tmp` is ephemeral and the service should run one instance. Use the existing Postgres blueprint for durable data.

## Public URL acceptance checks

1. Open the production URL in a private browser window; no login should be required.
2. Submit the assignment example or the Mumbai prompt in the smoke report. Confirm a final itinerary, real function trace, budget/time totals and source badges. The latest anonymous live check used Mumbai.
3. Submit an impossible budget and a vague request; confirm an honest outcome and useful clarification.
4. Temporarily test a model outage in a safe test environment; confirm the fallback is labelled. Do not expose fault controls publicly.
5. Open a share link and confirm the source names and uncertainty labels. Verify the deployment serves its latest eval summary.
6. Add the verified URL to the README and your submission message. A GitHub URL, local URL or unverified hostname is not the hosted UI.

## Submission message template

> Perfect Saturday Planner
>
> Live app: https://itinerary-planner-r54t.onrender.com/
> Source and local setup: https://github.com/Vatsalya001/itinerary_planner
>
> The app uses FastAPI, LangGraph and React, with separate preference parsing, location/weather retrieval, place search and plan-validation functions. It streams execution steps, computes budget/time in code and labels fallback plans when providers fail.
>
> AI tools: Claude Code helped with the initial implementation and scaffolding. Codex helped review the implementation, fix regressions and run tests/evaluations. The README records validation scope and known limitations.

Quota errors can return the labelled backup planner. The latest Cerebras smoke did complete in agent mode; retain the earlier fallback evidence separately and do not promise unlimited evaluator prompts. No Loom recording was created; it is optional.
