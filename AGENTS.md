# Engineering guide (humans and coding agents)

Read `IMPLEMENTATION_PLAN.md` (design) and `eval_cases.json` (behaviour spec) first. Shared contracts live in
`backend/app/domain/schemas.py` and `backend/app/providers/base.py`; module contracts are the stub signatures and
docstrings in each module you own.

## Phases

| Phase | Scope | Exit condition |
|---|---|---|
| 1. Deterministic core | Domain (evidence, travel, schedule, validator, scoring, fallback, infeasible), rules parser and clarification policy, data (frozen-world fixtures, OSM snapshot, city fares), providers (fixture, snapshot, Google Places, Open-Meteo, Tavily + JSON-LD events), baseline pipeline and CLI | Unit tests green; the no-LLM baseline produces valid plans for every feasible fixture case |
| 2. Agent and product | Model registry (Claude, Gemini, Grok, Ollama, scripted fake), LangGraph graph and tools, clarification `interrupt`, FastAPI SSE API, storage, share links, React UI | Offline graph tests green; a local Ollama model plans end to end; UI works locally |
| 3. Evals, hardening, docs | Eval harness and reports, `/evals` page, local-model eval runs, rate limits, security headers, logging, Sentry/LangSmith gating, Docker, Render blueprint, CI, HLD/LLD/README | Full suite green; docs verified against code |

## Rules

1. **Ownership.** Only edit files your task assigns to you. Never edit `schemas.py`, `providers/base.py`, `config.py`, `pyproject.toml` or another owner's module. If you need a contract change or a new dependency, stop and report it with a precise proposal in your final message.
2. **Units.** Local times are minutes since local midnight on the target date (ints, may exceed 1440). Durations are minutes. Money is whole currency units per person (ints). Currency is ISO 4217.
3. **Evidence.** Unknown is never yes. Hard constraints need `Evidence.CONFIRMED`.
4. **Numbers come from data.** Model or template prose never contains prices, durations or clock times.
5. **Data honesty.** Fixture attributes are synthetic and labelled so. The curated snapshot comes from OpenStreetMap (ODbL attribution, retrieval date) or official venue sites. Never copy Google Maps content into the repo.
6. **Tests.** Every module ships tests under `backend/tests/`. Unit tests never touch the network (use `respx` for HTTP). Tests that need network or keys are marked `@pytest.mark.live`; tests that need Ollama are marked `@pytest.mark.ollama`.
7. **Checks before you finish:** from `backend/`: `uv run pytest`, `uv run ruff check <your files>`, `uv run mypy <your files>`. Report the exact results, including failures, in your final message.
8. **No commits.** The coordinator reviews, verifies and commits.

## Commands

```bash
cd backend
uv run pytest                      # offline suite
uv run pytest -m live              # needs network/keys (RUN_LIVE=1)
uv run ruff check app tests
uv run mypy app
```
