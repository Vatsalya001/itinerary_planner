/**
 * Browser end-to-end tests against the real FastAPI backend serving the built frontend.
 *
 * The web server below builds the UI, then starts uvicorn from backend/ with the frozen world (fixture places,
 * events, weather and geocoder, fixed clock) and the scripted fake model, so the suite needs no keys and no network.
 * Rate limits are raised so the suite itself is not throttled (the 429 UI is tested with a mocked response that
 * copies the backend's real 429 shape).
 *
 *   npm run e2e                                  # build + start the backend + run every spec
 *   E2E_BASE_URL=http://127.0.0.1:8000 npm run e2e   # run against an already running server instead
 *
 * Requires `uv` (backend) and `npx playwright install chromium` once.
 */
import { defineConfig, devices } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const here = fileURLToPath(new URL('.', import.meta.url))
const frontendDir = fileURLToPath(new URL('..', import.meta.url))
const backendDir = fileURLToPath(new URL('../../backend', import.meta.url))
const port = Number(process.env.E2E_PORT ?? 8820)
const external = process.env.E2E_BASE_URL
const baseURL = external ?? `http://127.0.0.1:${port}`
const dataDir = process.env.E2E_DATA_DIR ?? '/tmp/psp-e2e'

const serverEnv = [
  'APP_ENV=test',
  'PLACES_PROVIDER=fixture',
  'EVENTS_PROVIDER=fixture',
  'WEATHER_PROVIDER=fixture',
  'GEOCODER=fixture',
  'FIXED_NOW=2026-11-05T04:30:00Z',
  'PRIMARY_MODEL=fake:agent',
  'FALLBACK_MODEL=',
  'RATE_LIMIT_PER_10MIN=10000',
  'RATE_LIMIT_PER_DAY=100000',
  'DAILY_PLAN_CAP=100000',
  `DATABASE_URL=sqlite:///${dataDir}/app.db`,
  `FRONTEND_DIST=${frontendDir}dist`,
].join(' ')

export default defineConfig({
  testDir: here,
  testMatch: /.*\.spec\.ts/,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    actionTimeout: 10_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: external
    ? undefined
    : {
        command:
          `npm run build --prefix "${frontendDir}" && rm -rf "${dataDir}" && mkdir -p "${dataDir}" && ` +
          `cd "${backendDir}" && env ${serverEnv} uv run uvicorn app.main:app --host 127.0.0.1 --port ${port}`,
        url: `${baseURL}/api/health`,
        reuseExistingServer: false,
        timeout: 180_000,
        stdout: 'ignore',
        stderr: 'pipe',
      },
})
