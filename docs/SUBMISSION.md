# Submission checklist

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
PRIMARY_MODEL=groq:openai/gpt-oss-120b
GROQ_API_KEY=<set privately in Render>
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

Enable Groq Developer billing and check the actual account token limits before sending real traffic. No live Groq acceptance test has been performed for this configuration change.

Optional: add another registered model with its provider key; add Google Places and event provider keys for wider data coverage. For the snapshot demo, tell reviewers that Bangalore is the supported city. Never label this snapshot as a live Places lookup.

Use `/api/health` as the health check. The image accepts the host-provided `PORT`. SQLite data in `/tmp` is ephemeral and the service should run one instance. Use the existing Postgres blueprint for durable data.

## Public URL acceptance checks

1. Open the production URL in a private browser window; no login should be required.
2. Submit the Bangalore example. Confirm a final itinerary, real function trace, budget/time totals and source badges.
3. Submit an impossible budget and a vague request; confirm an honest outcome and useful clarification.
4. Temporarily test a model outage in a safe test environment; confirm the fallback is labelled. Do not expose fault controls publicly.
5. Open a share link and confirm the source names and uncertainty labels. Verify the deployment serves its latest eval summary.
6. Add the verified URL to the README and your submission message. A GitHub URL, local URL or unverified hostname is not the hosted UI.
