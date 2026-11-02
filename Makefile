# Perfect Saturday Planner developer commands. Run `make` (or `make help`) to list them.
# Written for GNU make 3.81 (the version macOS ships) and bash. Needs uv and Node.js 22+.
#
# Configuration: backend/.env (copy .env.example). dev, serve and eval also pass it to `uv run --env-file`;
# the app itself exports provider keys from it at startup (Settings.export_provider_env). Shell variables win.
# test and lint do not export it (tests build their own settings), so they behave the same here and in CI.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BACKEND := backend
FRONTEND := frontend
UV ?= uv
# --frozen: use uv.lock as-is (never re-resolve or rewrite it from a make target)
UV_RUN := $(UV) run --frozen
# Same, plus backend/.env when it exists (expanded by the recipe shell after `cd backend`)
UV_RUN_ENV := $(UV_RUN) $$([ -f .env ] && echo "--env-file .env")

IMAGE ?= saturday-planner:local
PORT ?= 8000
WEB_PORT ?= 5173
# Extra arguments for pytest (test) or the eval runner (eval, eval-offline), e.g. ARGS="-k validator"
ARGS ?=
# Output directory for eval-offline (default: git-ignored scratch, so local runs don't touch committed results).
# Use OUT=evals/results to record an official offline session.
OUT ?= evals/results/tmp

# Evals (the harness lives in backend/evals/)
SUITE ?= models
MODELS ?= baseline:deterministic,ollama:qwen3.5:9b,ollama:gemma4:e4b,ollama:granite4:tiny-h
REPEATS ?= 1

# Local models for `make ollama` (Ollama tags, without the "ollama:" registry prefix)
OLLAMA_MODELS ?= qwen3.5:9b gemma4:e4b granite4:tiny-h
OLLAMA_BASE_URL ?= http://127.0.0.1:11434
OLLAMA_LOG ?= /tmp/ollama-serve.log

.PHONY: help setup dev serve test lint typecheck build ollama eval-offline eval eval-all eval-report \
	docker-build docker-run clean

help: ## Show this list of targets
	@echo "Usage: make <target> [VAR=value ...]"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN { FS = ":.*## " } { printf "  %-13s %s\n", $$1, $$2 }'
	@echo
	@echo "First run: make setup && make ollama && make dev, then open http://localhost:$(WEB_PORT)"

setup: ## Install backend (uv sync, incl. dev tools) and frontend (npm ci) dependencies
	@command -v $(UV) >/dev/null || { echo "uv is required: brew install uv (https://docs.astral.sh/uv/)"; exit 1; }
	@command -v npm >/dev/null || { echo "Node.js 22+ is required: brew install node"; exit 1; }
	cd $(BACKEND) && $(UV) sync --frozen
	cd $(FRONTEND) && npm ci --no-audit --no-fund
	@[ -f $(BACKEND)/.env ] || echo "Optional: cp .env.example $(BACKEND)/.env and edit it (keys, PRIMARY_MODEL, ...)"

# Both servers run as children of one bash; Ctrl+C (or either server exiting) stops both. The poll loop instead
# of `wait -n` keeps this working with macOS's bash 3.2.
dev: ## Backend on :8000 (uvicorn --reload) and frontend on :5173 (Vite, proxies /api); Ctrl+C stops both
	@[ -d $(FRONTEND)/node_modules ] || { echo "Run make setup first"; exit 1; }
	@if lsof -nP -iTCP:$(PORT) -sTCP:LISTEN >/dev/null 2>&1; then echo "Port $(PORT) is busy"; exit 1; fi
	@if lsof -nP -iTCP:$(WEB_PORT) -sTCP:LISTEN >/dev/null 2>&1; then echo "Port $(WEB_PORT) is busy"; exit 1; fi
	@api=; web=; \
	stop() { trap - INT TERM EXIT; [ -n "$$api" ] && kill $$api 2>/dev/null; [ -n "$$web" ] && kill $$web 2>/dev/null; \
		wait 2>/dev/null; echo "dev servers stopped"; }; \
	trap 'stop; exit 130' INT TERM; trap stop EXIT; \
	( cd $(BACKEND) && exec $(UV_RUN_ENV) uvicorn app.main:app --reload --reload-dir app \
		--host 127.0.0.1 --port $(PORT) ) & api=$$!; \
	( cd $(FRONTEND) && API_PROXY_TARGET=http://127.0.0.1:$(PORT) exec ./node_modules/.bin/vite \
		--host 127.0.0.1 --port $(WEB_PORT) --strictPort ) & web=$$!; \
	echo "api: http://127.0.0.1:$(PORT)/api/health   web: http://127.0.0.1:$(WEB_PORT)   (Ctrl+C stops both)"; \
	while kill -0 $$api 2>/dev/null && kill -0 $$web 2>/dev/null; do sleep 1; done; \
	echo "A dev server exited; stopping the other"; exit 1

serve: build ## Build the UI, then serve UI + API from one uvicorn on :8000, like production
	cd $(BACKEND) && FRONTEND_DIST="$(CURDIR)/$(FRONTEND)/dist" $(UV_RUN_ENV) uvicorn app.main:app \
		--host 127.0.0.1 --port $(PORT) --proxy-headers

test: ## Backend tests (offline: no network, keys or Ollama) and frontend unit tests (Vitest)
	cd $(BACKEND) && $(UV_RUN) pytest $(ARGS)
	cd $(FRONTEND) && CI=true npm test

lint: ## ruff (backend) and tsc + oxlint (frontend)
	cd $(BACKEND) && $(UV_RUN) ruff check .
	cd $(FRONTEND) && npm run lint

typecheck: ## mypy (backend app) and tsc (frontend)
	cd $(BACKEND) && $(UV_RUN) mypy app
	cd $(FRONTEND) && npm run typecheck

build: ## Build the production frontend into frontend/dist
	cd $(FRONTEND) && npm run build

ollama: ## Start Ollama if needed, pull missing local models (never re-pulls), tool-calling smoke test each
	@command -v ollama >/dev/null || { echo "Ollama is not installed: brew install ollama (or https://ollama.com/download)"; exit 1; }
	@if curl -fsS -m 3 "$(OLLAMA_BASE_URL)/api/version" >/dev/null 2>&1; then \
		echo " ok Ollama is running at $(OLLAMA_BASE_URL)"; \
	else \
		case "$(OLLAMA_BASE_URL)" in *127.0.0.1*|*localhost*) ;; \
			*) echo "Ollama is not reachable at $(OLLAMA_BASE_URL) (not local, so not starting it)"; exit 1;; esac; \
		echo "==> Starting 'ollama serve' in the background (log: $(OLLAMA_LOG))"; \
		nohup ollama serve >"$(OLLAMA_LOG)" 2>&1 & \
		for _ in $$(seq 1 30); do curl -fsS -m 2 "$(OLLAMA_BASE_URL)/api/version" >/dev/null 2>&1 && break; sleep 1; done; \
		curl -fsS -m 2 "$(OLLAMA_BASE_URL)/api/version" >/dev/null 2>&1 || { echo "Ollama did not start; see $(OLLAMA_LOG)"; exit 1; }; \
		echo " ok Ollama started"; \
	fi
	@export OLLAMA_HOST="$(OLLAMA_BASE_URL)"; for m in $(OLLAMA_MODELS); do \
		if ollama show "$$m" >/dev/null 2>&1; then echo " ok $$m is already pulled"; \
		else echo "==> Pulling $$m (one-time download)"; ollama pull "$$m" || exit 1; fi; \
	done
	@echo "==> Tool-calling smoke test (the first call loads each model into memory)"
	@fail=0; for m in $(OLLAMA_MODELS); do \
		python3 $(BACKEND)/scripts/ollama_smoke.py --model "$$m" --base-url "$(OLLAMA_BASE_URL)" || fail=1; \
	done; \
	if [ $$fail = 0 ]; then echo " ok Ollama is ready. Next: make dev"; else echo "Some models failed the smoke test"; exit 1; fi

# The eval harness (backend/evals/runner.py, report.py) is owned by the evals work; these targets only call it.
define require_eval
	@[ -f $(BACKEND)/evals/$(1).py ] || { echo "backend/evals/$(1).py not found: the eval harness is not in this checkout yet"; exit 2; }
endef

eval-offline: ## Offline eval suite (scripted fake model, frozen world; what CI runs)
	$(call require_eval,runner)
	cd $(BACKEND) && $(UV_RUN) python -m evals.runner --suite offline --out $(OUT) $(ARGS)

eval: ## Model evals on the frozen world: make eval MODELS=ollama:gemma4:e4b,anthropic:claude-opus-5-5 REPEATS=3
	$(call require_eval,runner)
	cd $(BACKEND) && $(UV_RUN_ENV) python -m evals.runner --suite $(SUITE) --models "$(MODELS)" --repeats $(REPEATS) $(ARGS)

eval-all: ## Model evals for the baseline, the local models, and Claude when ANTHROPIC_API_KEY is set
	@models="baseline:deterministic"; for m in $(OLLAMA_MODELS); do models="$$models,ollama:$$m"; done; \
	key="$${ANTHROPIC_API_KEY:-$$(sed -n 's/^ANTHROPIC_API_KEY=//p' $(BACKEND)/.env 2>/dev/null | tail -1)}"; \
	if [ -n "$$key" ]; then models="$$models,anthropic:claude-opus-5-5"; else echo "No ANTHROPIC_API_KEY: skipping Claude"; fi; \
	$(MAKE) --no-print-directory eval MODELS="$$models"

eval-report: ## Regenerate docs/EVAL_REPORT.md and evals/results/summary.json from the results
	$(call require_eval,report)
	cd $(BACKEND) && $(UV_RUN_ENV) python -m evals.report $(ARGS)

docker-build: ## Build the production image (IMAGE=saturday-planner:local)
	@command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/get-docker/"; exit 1; }
	docker build --build-arg GIT_SHA="$$(git rev-parse --short HEAD 2>/dev/null || echo dev)" -t $(IMAGE) .

# Paths and the port are pinned to the image layout, so host values in backend/.env cannot break the container.
# Ollama stays on the host (reached via host.docker.internal). Override the DB with DOCKER_DATABASE_URL.
docker-run: ## Run the image on :8000 with backend/.env; SQLite in the container unless DOCKER_DATABASE_URL is set
	@command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/get-docker/"; exit 1; }
	docker run --rm --name saturday-planner -p $(PORT):8000 \
		$$([ -f $(BACKEND)/.env ] && echo "--env-file $(BACKEND)/.env") \
		-e PORT=8000 -e FRONTEND_DIST=/app/frontend/dist \
		-e DATABASE_URL="$${DOCKER_DATABASE_URL:-sqlite:////app/var/app.db}" \
		-e OLLAMA_BASE_URL="$${DOCKER_OLLAMA_BASE_URL:-http://host.docker.internal:11434}" \
		--add-host host.docker.internal:host-gateway $(IMAGE)

clean: ## Remove build output and caches (keeps .venv, node_modules and local databases)
	rm -rf $(FRONTEND)/dist $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache $(BACKEND)/.mypy_cache
	find $(BACKEND) -path $(BACKEND)/.venv -prune -o -type d -name __pycache__ -prune -exec rm -rf {} +
