# syntax=docker/dockerfile:1

# Perfect Saturday Planner production image: one container serves the FastAPI API and the built React UI from
# the same origin (no CORS). Configure it with environment variables (see .env.example).
#
#   docker build --build-arg GIT_SHA="$(git rev-parse --short HEAD)" -t saturday-planner .
#   # Offline smoke run: frozen-world fixtures, no LLM, in-memory SQLite, nothing leaves the container
#   docker run --rm -p 8000:8000 -e APP_ENV=test -e PRIMARY_MODEL=baseline:deterministic \
#     -e PLACES_PROVIDER=fixture -e EVENTS_PROVIDER=fixture -e WEATHER_PROVIDER=fixture -e GEOCODER=fixture \
#     -e DATABASE_URL=sqlite:///:memory: saturday-planner
#
# Layout inside the image (the backend resolves its data files relative to its own package):
#   /app/.venv            Python dependencies, installed by uv from backend/uv.lock (no dev group)
#   /app/backend          app/, data/, evals/ (eval summary for /api/evals/summary); the working directory
#   /app/frontend/dist    the built UI (FRONTEND_DIST; also what the default "../frontend/dist" resolves to)
#   /app/var              the only writable directory: default SQLite app DB and LangGraph checkpoints

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22
ARG UV_VERSION=0.13.6

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---- Stage 1: build the frontend ---------------------------------------------------------------------------
FROM node:${NODE_VERSION}-slim AS frontend
WORKDIR /build/frontend
ENV CI=true
# Dependencies first so this layer stays cached until the lockfile changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: resolve and install Python dependencies with uv ------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps
COPY --from=uv /uv /usr/local/bin/uv
# The venv lives at the same absolute path in this stage and the runtime, so its scripts keep working.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON=/usr/local/bin/python3
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
# --frozen: install exactly what uv.lock pins (never re-resolve); --no-dev: no test/lint tools in production.
RUN uv sync --frozen --no-dev --no-install-project

# ---- Stage 3: runtime ----------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# Baked in by CI (`--build-arg GIT_SHA=...`); at runtime GIT_SHA, then Render's RENDER_GIT_COMMIT, win over it.
ARG GIT_SHA=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    APP_ENV=prod \
    PORT=8000 \
    FRONTEND_DIST=/app/frontend/dist \
    DATABASE_URL=sqlite:////app/var/app.db \
    BUILD_GIT_SHA=${GIT_SHA}

# Unprivileged runtime user (uid/gid 1000). Only /app/var is writable by it; code and data stay root-owned.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/var \
    && chown app:app /app/var

WORKDIR /app/backend
COPY --from=deps /app/.venv /app/.venv
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/app ./app
COPY backend/data ./data
COPY backend/evals ./evals
COPY --from=frontend /build/frontend/dist /app/frontend/dist

# Precompile the app as root (the runtime user cannot write __pycache__, so cold starts would recompile it).
RUN python -m compileall -q app

USER 1000:1000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8000'), timeout=4)"]

# One async worker: in-flight runs are drained on SIGTERM (up to RUN_DEADLINE_S + 10 s), so give the container
# that long to stop (compose stop_grace_period, Render maxShutdownDelaySeconds).
# Hosts such as Render inject PORT. --proxy-headers only trusts the loopback proxy (uvicorn's default
# FORWARDED_ALLOW_IPS); behind Render the app itself takes the client IP from the right-most X-Forwarded-For hop
# when TRUST_PROXY_HEADERS=true (app/api/security.py). Do not add --forwarded-allow-ips='*': uvicorn would then
# trust the left-most, client-supplied hop. `exec` makes uvicorn PID 1 so it receives SIGTERM directly.
CMD ["sh", "-c", "export GIT_SHA=\"${GIT_SHA:-${RENDER_GIT_COMMIT:-${BUILD_GIT_SHA:-dev}}}\"; exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --proxy-headers"]
