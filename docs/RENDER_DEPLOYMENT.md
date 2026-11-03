# Deploy Perfect Saturday Planner on Render

Use the existing `render.yaml`: one Docker web service serves the UI and API, with Neon Postgres for persistent plans and clarification state. Groq GPT-OSS 120B is the primary model; model failures use the validated deterministic planner. Deployment has not been performed yet.

## 1. Prepare Groq

1. Sign in at https://console.groq.com.
2. Select the organization/project you will use for this app.
3. In account billing, upgrade to the paid Developer tier and add a payment method. Set a spend limit suitable for your demo.
4. Check the organization's Limits page for `openai/gpt-oss-120b`; do not assume that a key alone enables paid limits.
5. Use or create an API key in that organization. Keep it private; enter it as `GROQ_API_KEY` on Render, not in GitHub source or chat.

The application has a conservative local 30-model-calls/minute ceiling. This does not replace Groq's token limits. No hosted-model acceptance call was made during the configuration switch.

Official references: https://console.groq.com/docs/billing-faqs and https://console.groq.com/docs/rate-limits.

## 2. Create persistent Postgres in Neon

1. Sign in at https://console.neon.tech.
2. Create a Postgres project called `saturday-planner`. Prefer AWS Singapore (`ap-southeast-1`) when available, to match the Render service.
3. Use the default database/role and your production branch, or create a dedicated database/role.
4. Click Connect, choose that database/role, and copy the Postgres connection string. A pooled hostname containing `-pooler` is supported by this app.
5. Preserve `sslmode=require` and any other connection parameters Neon supplies.
6. Save the string privately. It will become Render's `DATABASE_URL`.

Example SHAPE only (replace with the full real URL, without shell quotes):

```text
postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
```

The app creates its own storage and checkpoint tables on startup. You do not need to run a local migration command. Keep the local development database separate from production.

Reference: https://neon.com/docs/connect/connect-from-any-app.

## 3. Check the GitHub source

Open https://github.com/Vatsalya001/itinerary_planner and select branch `main`. In Actions, wait for the CI run for the Groq-switch commit to succeed. The manual hosted-model eval job can be skipped on pushes; that is expected. Backend, frontend, Postgres and Docker jobs should pass.

## 4. Create the Render Blueprint

1. Sign in at https://dashboard.render.com using your GitHub account.
2. Choose New → Blueprint.
3. Connect GitHub and grant Render access to `Vatsalya001/itinerary_planner`. If the repo is missing, update Render's GitHub app repository access.
4. Choose that repository, branch `main`, and the root blueprint file `render.yaml`.
5. Name the Blueprint `saturday-planner` (or another convenient name).
6. Review the proposed service: Docker, Singapore, Starter plan. Starter is a paid service; review the displayed charge before creating it.
7. Enter the private environment values described below, then deploy the Blueprint.

The blueprint contains the configuration; do not create a second Python service or a separate static frontend. The root Dockerfile builds both parts. No custom build/start command is needed.

References: https://render.com/docs/infrastructure-as-code and https://render.com/docs/blueprint-spec.

## 5. Enter secret values and confirm runtime settings

Render prompts for blueprint variables marked `sync: false` at initial creation.

Required:

| Key | Value |
| --- | --- |
| `GROQ_API_KEY` | Your key from the paid Groq organization |
| `DATABASE_URL` | The full Neon Postgres URL |

For multi-city real-place results, also set `GOOGLE_MAPS_API_KEY` to a server-side key with Places API (New) enabled and its billing configured. Without that key, `PLACES_PROVIDER=auto` uses the Bangalore snapshot; do not expect arbitrary cities to have venue coverage. You can reuse your existing eligible key privately. Avoid browser-referrer-only restrictions for a server-side API key.

Optional values can remain empty/unset: `TAVILY_API_KEY` (event discovery), `JUDGE_MODEL`, `SENTRY_DSN`, and `LANGSMITH_API_KEY`. The judge is not used by the app. Keep `TRACING_ENABLED=false` for this initial deployment.

In the service's Environment page, confirm the non-secret values supplied by `render.yaml`:

```dotenv
APP_ENV=prod
PRIMARY_MODEL=groq:openai/gpt-oss-120b
FALLBACK_MODEL=
FRONTEND_DIST=/app/frontend/dist
TRUST_PROXY_HEADERS=true
PLACES_PROVIDER=auto
EVENTS_PROVIDER=auto
WEATHER_PROVIDER=open_meteo
GEOCODER=open_meteo
RUN_DEADLINE_S=60
MAX_AGENT_TURNS=10
MAX_SUBMIT_ATTEMPTS=3
DAILY_PLAN_CAP=200
RATE_LIMIT_PER_10MIN=15
RATE_LIMIT_PER_DAY=60
TRACING_ENABLED=false
```

Keep `FALLBACK_MODEL` empty. Do not use Ollama, fixture providers, `FIXED_NOW`, or `FAULT` for this deployment. Do not bulk-copy the entire local `.env`: it contains development settings. Render supplies the listening port; the Dockerfile binds correctly to it.

If you change environment values later, choose Save and deploy (or Save, rebuild, and deploy). Save only does not apply them to the currently running process.

Reference: https://render.com/docs/configure-environment-variables.

## 6. Watch the deployment

1. Open the `saturday-planner` web service and its Events/Logs view.
2. Confirm the deployed commit is the intended latest `main` commit.
3. Follow the Docker build, app startup and health check until the deploy is Live.
4. Copy the service's actual HTTPS `onrender.com` URL. Do not guess its hostname.

The health-check path is `/api/health`. The image builds the frontend and installs locked Python dependencies. Startup initializes the Postgres store/checkpointer. No shell migration or `make dev` command is needed on Render.

Reference: https://render.com/docs/docker and https://render.com/docs/health-checks.

## 7. Verify the public URL

Open the URL in a private browser window, then open `/api/health` on that same host. It should return HTTP 200 with at least:

```json
{
  "status": "ok",
  "env": "prod",
  "primary_model": "groq:openai/gpt-oss-120b",
  "db": "ok"
}
```

Other fields will also be present. Health verifies storage, not whether a paid model call succeeds.

In the UI:

1. Submit the assignment Bangalore example and inspect the model/mode label, trace, costs, time and sources. A successful live-AI acceptance run should finish in agent mode with Groq, rather than silently relying on backup mode.
2. Submit the original Delhi and London prompts; the previously misclassified response instructions should not trigger clarification. Honest lack of candidate/accessibility evidence can still prevent an itinerary.
3. Try an impossible budget and a vague request; verify a useful infeasibility response/clarification.
4. Open a share link in a private window. Redeploy once and verify that it still opens, demonstrating persistent storage.
5. Try mobile layout, keyboard navigation and stop/retry.

You can check the API from a terminal (replace YOUR-SERVICE with the real hostname):

```bash
curl -i https://YOUR-SERVICE.onrender.com/api/health
curl -N --max-time 90 https://YOUR-SERVICE.onrender.com/api/plan \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  --data '{"city":"Bangalore","budget":2000,"available_time":"4 hours","mood":"tired but wants to do something fun","interests":["food","music","walks"],"constraints":["vegetarian","avoid crowded places"]}'
```

The second command invokes the configured paid model. Check trace events and the final result. `/docs` is disabled; do not use it as a health check. Production OpenAPI is disabled too.

## 8. Troubleshooting

| Symptom | Check |
| --- | --- |
| Build fails | Render build log and the same commit's GitHub Docker job; repository root and root Dockerfile must be used |
| Startup/health fails | Full Neon URL, SSL parameters, database credentials, network availability, and startup logs |
| Groq 401 | Key is valid and belongs to the intended organization/project |
| Groq 429 or rate-limit fallback | Paid tier, actual token/request quotas, remaining spend allowance, and concurrent traffic; inspect the trace before attributing it to a code bug |
| Backup plan rather than agent plan | Degradation text/trace: provider error, timeout, invalid submissions or exhausted turns can each trigger fallback |
| Only Bangalore has venues | Configure eligible Google Places access; without it, the snapshot has limited coverage |
| Places auth failure | Places API (New), Cloud billing and key restrictions; do not confuse the Maps key with the Groq key |
| Env edit has no effect | Save and deploy, then verify the new deployment in Events |
| New commit does not auto-deploy | Check GitHub CI: blueprint auto-deploy waits for checks to pass |

Do not post API keys, database URLs, or unredacted logs publicly. Keep the service at one instance initially: the model-call limiter is process-local. Before claiming production readiness, confirm the public-URL acceptance checks and actual provider behaviour.

## 9. Finish the assignment submission

Add the verified live URL to README.md and docs/SUBMISSION.md, then submit the URL and GitHub repo. The README already contains setup, architecture, eval evidence and AI-use disclosure. Use docs/DEMO_SCRIPT.md for an optional short recording. A deployment that is Live but always produces a provider-error fallback is not a validated live-AI demo.
