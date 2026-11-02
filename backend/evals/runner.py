"""Eval runner: ``python -m evals.runner --suite offline|models|live [--models a,b] [--repeats N]``.

For each selected case x model x repeat it builds frozen-world settings (case fault, weather, config overrides,
frozen clock from fixed_context, run deadline by model kind), installs the case's event scenarios, runs
``app.graph.runner.run_plan`` (resuming the same thread with the case's clarification answers when it pauses), or
drives the HTTP layer through ``create_app`` with an httpx ASGI client for the HTTP-layer cases. Each run is checked by
``evals.assertions`` (and optionally scored by ``evals.judge``) and written as one JSONL record to
``evals/results/<UTC date>/<model>.jsonl``. Unavailable models (pending registry IDs, missing API keys, Ollama down or
model not pulled) are skipped with a recorded reason. Failed or throttled runs are recorded, never rerun.

A harness exception (a bug in the harness rather than in the planner) still writes a record: status ``error``, error
category ``harness_exception``, no assertions, counted as a hard failure, so the run never vanishes from the metrics.

Environment: at start the CLI loads backend/.env through app.config.Settings and exports the model-provider keys it
finds into the process environment (``Settings.export_provider_env``; values already set win), so running without
make's ``--env-file`` sees the same keys. ``JUDGE_MODEL`` is the default judge for the models and live suites;
``--judge-model`` wins and ``--judge-model none`` disables it (the offline suite is judged only on request).

Long local runs: every record is appended (and the file closed) as soon as its run finishes, so an interrupted run
loses at most the run in progress. ``--resume`` continues the latest session of each model in ``--out`` when it was
recorded with the same record schema, git SHA, dirty-tree flag, prompt, registry, fixture, eval_cases and harness
hashes: (case, repeat) pairs already recorded there are skipped, whether they passed or failed (failed runs are never
rerun), and new records join the same session so the report sees one complete session. The dirty-tree flag ignores
generated eval output (results, the report, the --out directory) and agent tooling state under .claude/.
``--max-minutes`` caps the wall time spent on each model in this invocation and ``--max-cost-usd`` the estimated
cost (planner plus judge, registry pricing) summed over every model in this invocation; both are checked between
runs, and when one runs out the remaining runs are recorded as a ``budget_stop`` and can be finished with
``--resume``. Progress lines show an ETA from the mean time per run so far.

Exit status: 2 if the harness itself failed; 1 if the gate is on and a run failed a hard check (the offline suite is
deterministic, so it gates by default; disable with ``--no-gate``; models and live gate only with ``--gate``); 3 if a
model stopped early on ``--max-minutes`` or ``--max-cost-usd`` (finish with ``--resume``); 130 on Ctrl-C (finish with
``--resume``); else 0.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import traceback
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evals import BACKEND_DIR, DATE_DIR, RECORD_SCHEMA, REPO_ROOT, REPORT_PATH, RESULTS_DIR
from evals.assertions import (
    SENTINEL_SETTINGS,
    Check,
    Observation,
    Phase,
    evaluate,
    finalize_data,
    hard_pass,
    parse_fields,
    plan_digest,
    status_ok,
)
from evals.cases import (
    SPLITS,
    SUITES,
    CallLog,
    EvalCase,
    build_case_deps,
    case_settings,
    deadline_s,
    default_start_min,
    fixture_hash,
    harness_hash,
    install_recorders,
    load_cases,
    load_spec,
    model_kind,
    prompt_hash,
    registry_hash,
    request_data,
    select_cases,
    spec_hash,
    target_date,
)
from evals.metrics import HARNESS_EXCEPTION, record_schema

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger("evals")

ENV_FILE = BACKEND_DIR / ".env"
HARNESS_GRACE_S = 30  # on top of the run deadline (the graph runner hard-stops 5 s after it)
CLIENT_IP = "203.0.113.77"
NO_FRONTEND = str(BACKEND_DIR / "evals" / "_no_frontend")
KEY_FIELDS = {"anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
              "google_genai": ("google_api_key", "GOOGLE_API_KEY"),
              "xai": ("xai_api_key", "XAI_API_KEY"),
              "groq": ("groq_api_key", "GROQ_API_KEY"),
              "cerebras": ("cerebras_api_key", "CEREBRAS_API_KEY")}
SECRET_FIELDS = ("anthropic_api_key", "google_api_key", "xai_api_key", "groq_api_key", "cerebras_api_key",
                 "langsmith_api_key",
                 "google_maps_api_key",
                 "tavily_api_key")
NO_JUDGE = frozenset({"", "none", "off"})  # --judge-model / JUDGE_MODEL values that mean "no judge"


# ---------------------------------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    suite: str
    spec: dict[str, Any]
    out_dir: Path
    date: str
    extra: dict[str, Any] = field(default_factory=dict)  # settings overrides for every run (keys, base URLs)
    secrets: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    judge: Any = None
    judge_unavailable: dict[str, Any] | None = None
    verbose: bool = False
    model_sessions: dict[str, str] = field(default_factory=dict)  # resumed sessions, per model

    def session_for(self, model_id: str) -> str:
        return self.model_sessions.get(model_id, self.id)

    @property
    def live(self) -> bool:
        return self.suite == "live"

    def path_for(self, model_id: str) -> Path:
        return self.out_dir / self.date / f"{sanitize(model_id)}.jsonl"

    def write(self, model_id: str, record: dict[str, Any]) -> None:
        path = self.path_for(model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def sanitize(model_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model_id).strip("_") or "model"


RESUME_KEYS = ("git_sha", "git_dirty", "prompt_hash", "registry_hash", "fixture_hash", "eval_cases_hash",
               "harness_hash")


@dataclass
class ResumePoint:
    session: str | None
    done: set[tuple[str, int]]
    note: str


def resume_point(out_dir: Path, suite: str, model_id: str, provenance: dict[str, Any]) -> ResumePoint:
    """The latest session of ``model_id`` in ``out_dir`` and the (case, repeat) pairs it already recorded, when it was
    recorded with the same provenance; otherwise nothing to resume (a new session starts)."""
    recs: list[dict[str, Any]] = []
    for path in sorted(out_dir.glob(f"*/{sanitize(model_id)}.jsonl")):
        if not DATE_DIR.match(path.parent.name):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (rec.get("record_type", "run") in ("run", "budget_stop") and rec.get("suite") == suite
                    and rec.get("model") == model_id):
                recs.append(rec)
    if not recs:
        return ResumePoint(None, set(), "no earlier runs to resume")
    latest = max(str(r.get("session")) for r in recs)
    mine = [r for r in recs if str(r.get("session")) == latest]
    differs = ["record schema"] if record_schema(mine[0]) != RECORD_SCHEMA else []
    differs += [k for k in RESUME_KEYS if mine[0].get(k) != provenance.get(k)]
    if differs:
        return ResumePoint(None, set(), f"latest session {latest} was recorded with a different {', '.join(differs)}; "
                                        "starting a new session")
    done = {(str(r.get("case")), int(r.get("repeat") or 1)) for r in mine if r.get("record_type", "run") == "run"}
    return ResumePoint(latest, done, f"resuming session {latest}: {len(done)} run(s) already recorded")


def fmt_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def generated_paths(out_dir: Path | None = None) -> list[Path]:
    """Paths whose changes never make the tree dirty: eval output written while runs are in progress (results, the
    generated report, the --out directory) and agent tooling state (.claude/, which holds agent worktrees)."""
    paths = [RESULTS_DIR, REPORT_PATH, REPO_ROOT / ".claude"]
    if out_dir is not None:
        paths.append(out_dir)
    return paths


def porcelain_paths(status: str) -> list[str]:
    """Every path in ``git status --porcelain -z`` output (both sides of a rename or copy)."""
    items = status.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:  # -z puts the source path in the next field
            if i < len(items) and items[i]:
                paths.append(items[i])
            i += 1
    return paths


def tree_dirty(status: str, ignored: list[str]) -> bool:
    """True when ``git status --porcelain -z`` shows a change outside the ``ignored`` repo-relative paths."""
    def is_ignored(path: str) -> bool:
        p = path.rstrip("/")
        return any(p == ig or p.startswith(ig + "/") for ig in ignored)

    return any(not is_ignored(p) for p in porcelain_paths(status))


def git_info(root: Path = REPO_ROOT, ignore: list[Path] | None = None) -> tuple[str, bool | None]:
    """(short SHA, dirty) where dirty ignores changes confined to ``ignore`` (default: generated_paths())."""

    def git(*cmd: str) -> str:
        return subprocess.run(["git", *cmd], cwd=root, capture_output=True, text=True, timeout=10,
                              check=True).stdout

    try:
        sha = git("rev-parse", "--short=12", "HEAD").strip()
        top = Path(git("rev-parse", "--show-toplevel").strip()).resolve()
        ignored = []
        for path in generated_paths() if ignore is None else ignore:
            with contextlib.suppress(ValueError):  # outside this repository: nothing to ignore
                ignored.append(path.resolve().relative_to(top).as_posix())
        # -uall lists untracked files one by one, so an untracked parent directory is never collapsed into one
        # entry that would hide whether everything under it is ignored output
        return sha or "unknown", tree_dirty(git("status", "--porcelain", "-z", "--untracked-files=all"), ignored)
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("GIT_SHA", "unknown"), None


def env_settings(env_file: Path | None = None) -> Settings | None:
    """app.config.Settings from the process environment plus backend/.env (when present); None when invalid."""
    path = env_file or ENV_FILE
    try:
        from app.config import Settings

        return Settings(_env_file=str(path) if path.is_file() else None)
    except Exception:
        return None


def load_env(env_file: Path | None = None) -> Settings | None:
    """Runner start: read backend/.env and export the model-provider keys it holds into os.environ
    (Settings.export_provider_env, which never overwrites), so availability checks, LangChain clients and the live
    suite see keys that are only in .env when the runner is started without make's --env-file."""
    settings = env_settings(env_file)
    if settings is not None:
        settings.export_provider_env()
    return settings


def resolve_judge_model(flag: str | None, suite: str, configured: str | None) -> str:
    """The judge model for this invocation ("" for none). An explicit --judge-model wins ("none" disables the judge);
    otherwise JUDGE_MODEL (Settings.judge_model) for the models and live suites. The offline suite (scripted fake
    plans, no network) is judged only when --judge-model is given."""
    if flag is not None:
        return "" if flag.strip().lower() in NO_JUDGE else flag.strip()
    if suite == "offline" or configured is None or configured.strip().lower() in NO_JUDGE:
        return ""
    return configured.strip()


def env_settings_values() -> dict[str, Any]:
    """Model-provider keys and the Ollama URL from the environment or backend/.env (via app.config.Settings)."""
    out: dict[str, Any] = {}
    env = env_settings()
    if env is None:
        return out
    for name in ("anthropic_api_key", "google_api_key", "xai_api_key", "groq_api_key", "cerebras_api_key",
                 "langsmith_api_key"):
        value = getattr(env, name, None)
        if value is not None and value.get_secret_value():
            out[name] = value.get_secret_value()
    out["ollama_base_url"] = env.ollama_base_url
    return out


# ---------------------------------------------------------------------------------------------------
# Availability (skips are recorded, never counted as failures)
# ---------------------------------------------------------------------------------------------------


def ollama_models(base_url: str, timeout: float = 3.0) -> list[str]:
    import httpx

    resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
    resp.raise_for_status()
    return [str(m.get("name") or m.get("model") or "") for m in resp.json().get("models") or []]


def unavailable_reason(model_id: str, extra: dict[str, Any]) -> str | None:
    """None when the model can run here, else why it is skipped."""
    from app.llm.registry import PendingModelError, RegistryError, get_spec, is_baseline

    if is_baseline(model_id):
        return None
    try:
        spec = get_spec(model_id)
    except PendingModelError as exc:
        return f"pending registry ID: {exc}"
    except RegistryError as exc:
        return f"not runnable: {exc}"
    if spec.kind in ("fake", "baseline"):
        return None
    if spec.provider in KEY_FIELDS:
        field_name, env_name = KEY_FIELDS[spec.provider]
        if not (extra.get(field_name) or os.environ.get(env_name)):
            return f"{env_name} is not set"
        return None
    if spec.provider == "ollama":
        base = str(extra.get("ollama_base_url") or "http://127.0.0.1:11434")
        try:
            names = ollama_models(base)
        except Exception as exc:
            return f"Ollama server not reachable at {base} ({type(exc).__name__})"
        if spec.model not in names and f"{spec.model}:latest" not in names:
            return f"Ollama model {spec.model} is not pulled (run `ollama pull {spec.model}`)"
        return None
    return None


# ---------------------------------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------------------------------


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


async def thread_paused(deps: Any, thread_id: str) -> bool | None:
    """True when the thread is checkpointed at ask_clarification (so a resume would continue it)."""
    from app.graph.runner import checkpointer_for, compiled_graph

    try:
        graph = compiled_graph(checkpointer_for(deps))
        snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return "ask_clarification" in (snap.next or ())
    except Exception:
        return None


def live_deps(model_id: str, kind: str, sess: Session) -> tuple[Any, CallLog]:
    from langgraph.checkpoint.memory import InMemorySaver

    from app.config import Settings
    from app.deps import build_deps

    env_file = ENV_FILE
    settings = Settings(_env_file=str(env_file) if env_file.is_file() else None, primary_model=model_id,
                        run_deadline_s=deadline_s(kind, sess.spec), fault=None)
    deps = build_deps(settings)
    deps.checkpointer = InMemorySaver()
    return deps, install_recorders(deps)


async def run_graph_case(case: EvalCase, model_id: str, kind: str, data: dict[str, Any], sess: Session) -> Observation:
    from app.domain.schemas import PlanRequest
    from app.graph.runner import run_plan

    if sess.live:
        deps, call_log = live_deps(model_id, kind, sess)
    else:
        deps, call_log = build_case_deps(case, case_settings(case, model_id, sess.spec, sess.extra))
    settings = deps.settings
    obs = Observation(case=case, model=model_id, kind=kind, data=data, phases=[], call_log=call_log,
                      deadline_s=settings.run_deadline_s, secrets=sess.secrets, target=None if sess.live else
                      target_date(sess.spec), default_start=default_start_min(sess.spec), live=sess.live)
    limit = settings.run_deadline_s + HARNESS_GRACE_S
    t0 = time.perf_counter()
    try:
        request = PlanRequest.model_validate(data)
        t = time.perf_counter()
        result, events = await asyncio.wait_for(run_plan(request, deps, model_id=model_id), timeout=limit)
        obs.phases.append(Phase(result, events, _ms(t)))
        answers = case.clarification_answers
        if answers and result.status == "needs_clarification" and result.thread_id:
            t = time.perf_counter()
            result, events = await asyncio.wait_for(
                run_plan(None, deps, thread_id=result.thread_id, resume_answers=dict(answers), model_id=model_id),
                timeout=limit)
            obs.phases.append(Phase(result, events, _ms(t), answers=dict(answers)))
        final = obs.phases[-1].result
        if final is not None and final.status == "needs_clarification" and final.thread_id:
            obs.extra["resumable"] = await thread_paused(deps, final.thread_id)
        if final is not None and not sess.live and data.get("city") and any(
                q.id == "city_not_found" for q in final.questions):
            from app.providers.fixture import FixtureGeocoder

            obs.extra["geocoder_matches"] = len(await FixtureGeocoder().resolve(str(data["city"])))
    except TimeoutError:
        obs.error = f"harness_timeout: no result within {limit} s"
    except Exception as exc:
        obs.error = f"harness_error: {type(exc).__name__}: {exc}"[:500]
        if sess.verbose:
            traceback.print_exc()
    # End-to-end planner time (both phases of an ask-then-resume case); harness probes are excluded.
    obs.latency_ms = sum(p.latency_ms for p in obs.phases) if obs.phases and not obs.error else _ms(t0)
    return obs


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@contextlib.contextmanager
def count_planner_runs() -> Iterator[dict[str, int]]:
    """Count planner runs started through app.graph.runner (the API calls it by module attribute)."""
    import app.graph.runner as runner_module

    original = runner_module.stream_plan
    counter = {"runs": 0}

    async def counted(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        counter["runs"] += 1
        async for event in original(*args, **kwargs):
            yield event

    setattr(runner_module, "stream_plan", counted)  # noqa: B010 - patched for the duration of one HTTP case
    try:
        yield counter
    finally:
        setattr(runner_module, "stream_plan", original)  # noqa: B010


def stored_record_problems(record: dict[str, Any] | None, data: dict[str, Any]) -> list[str]:
    """Share-link storage rules: no free text, no resume handle, no Google display fields."""
    if record is None:
        return ["record not found in the store"]
    problems: list[str] = []
    blob = json.dumps(record, ensure_ascii=False)
    res = record.get("result") or {}
    if res.get("thread_id"):
        problems.append("thread_id stored")
    prefs = res.get("preferences") or {}
    for key in ("mood", "soft_preferences"):
        if prefs.get(key):
            problems.append(f"preferences.{key} stored")
    if any(c.get("source_text") for c in prefs.get("hard_constraints") or []):
        problems.append("constraint source_text stored")
    for text in [data.get("mood"), data.get("free_text"), *(data.get("constraints") or [])]:
        # multi-word user phrases only: single words such as "vegetarian" are also vocabulary labels in computed fields
        if isinstance(text, str) and len(text) >= 12 and " " in text.strip() and text in blob:
            problems.append(f"request text stored verbatim: {text[:40]!r}")
    plan = res.get("plan") or {}
    for stop in plan.get("stops") or []:
        for cand in (stop.get("candidate"), stop.get("plan_b")):
            if isinstance(cand, dict) and (cand.get("source") or {}).get("provider") == "google_places" and (
                    cand.get("name") or cand.get("address") or cand.get("rating") is not None
                    or cand.get("maps_url")):
                problems.append(f"Google display fields stored for {cand.get('id')}")
    return problems


async def run_http_case(case: EvalCase, model_id: str, kind: str, data: dict[str, Any], sess: Session) -> Observation:
    import httpx

    from app.domain.schemas import PlanResult
    from app.main import create_app
    from app.store.sqlite import SQLiteStore

    settings = case_settings(case, model_id, sess.spec, {**sess.extra, "frontend_dist": NO_FRONTEND})
    deps, call_log = build_case_deps(case, settings)
    clock = _Clock(datetime.now(UTC))
    store = SQLiteStore(":memory:", clock=clock)
    await store.init()
    deps.store = store
    obs = Observation(case=case, model=model_id, kind=kind, data=data, phases=[], call_log=call_log,
                      deadline_s=settings.run_deadline_s, secrets=sess.secrets, target=target_date(sess.spec),
                      default_start=default_start_min(sess.spec))
    http: dict[str, Any] = {"bodies": []}
    t0 = time.perf_counter()

    def record_response(resp: httpx.Response) -> Any:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:500]
        http.update(status_code=resp.status_code, headers=dict(resp.headers), body=body)
        http["bodies"].append(body)
        return body

    try:
        app = create_app(settings, deps)
        with count_planner_runs() as counter:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=(CLIENT_IP, 4321))
                async with httpx.AsyncClient(transport=transport, base_url="http://evals.test",
                                             timeout=settings.run_deadline_s + HARNESS_GRACE_S) as client:
                    if case.fault == "exceed_per_ip_limit":
                        sent = 0
                        for _ in range(settings.rate_limit_per_10min + 1):
                            before = counter["runs"]
                            resp = await client.post("/api/plan", json=data)
                            sent += 1
                            if resp.status_code == 429:
                                http["planner_runs_limited_request"] = counter["runs"] - before
                                break
                        record_response(resp)
                        http["requests_sent"] = sent
                    else:
                        t = time.perf_counter()
                        resp = await client.post("/api/plan", content=json.dumps(data).encode("utf-8"),
                                                 headers={"content-type": "application/json"})
                        body = record_response(resp)
                        if resp.status_code == 200 and isinstance(body, dict) and "run_id" in body:
                            result = PlanResult.model_validate(body)
                            obs.phases.append(Phase(result, [], _ms(t)))
                            if case.id == "share_link_roundtrip":
                                await _share_roundtrip(client, store, clock, settings, result, data, http)
            http["planner_runs"] = counter["runs"]
    except Exception as exc:
        obs.error = f"harness_error: {type(exc).__name__}: {exc}"[:500]
        if sess.verbose:
            traceback.print_exc()
    finally:
        await store.close()
    obs.http = http
    obs.latency_ms = _ms(t0)
    return obs


async def _share_roundtrip(client: Any, store: Any, clock: _Clock, settings: Any, result: Any,
                           data: dict[str, Any], http: dict[str, Any]) -> None:
    from app.domain.schemas import PlanResult

    http["plan_digest"] = plan_digest(result)
    share_id = result.share_id
    if not share_id:
        return
    got = await client.get(f"/api/plans/{share_id}")
    http["share_status"] = got.status_code
    if got.status_code == 200:
        http["shared_digest"] = plan_digest(PlanResult.model_validate(got.json()))
    http["record_problems"] = stored_record_problems(await store.get_plan(share_id), data)
    unknown = await client.get("/api/plans/unknownShare1")
    http["unknown_status"] = unknown.status_code
    with contextlib.suppress(ValueError):
        http["not_found_detail"] = str(unknown.json().get("detail"))
    clock.now = clock.now + timedelta(days=settings.share_ttl_days + 1)
    expired = await client.get(f"/api/plans/{share_id}")
    http["expired_status"] = expired.status_code


# ---------------------------------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------------------------------


def error_categories(obs: Observation) -> list[str]:
    from app.graph.nodes.common import (
        AGENT_NO_SUBMIT,
        AGENT_NO_TIME,
        AGENT_NOT_CONFIGURED,
        AGENT_RATE_LIMITED,
        AGENT_REFUSED,
        AGENT_REPAIRS_EXHAUSTED,
        AGENT_TIMEOUT,
        AGENT_TURN_CAP,
        AGENT_UNAVAILABLE,
        PARSER_DEGRADED,
    )
    from app.graph.runner import GENERIC_ERROR, TOO_SLOW

    mapping = [(AGENT_TIMEOUT, "model_timeout"), (AGENT_REFUSED, "model_refusal"),
               (AGENT_UNAVAILABLE, "model_unavailable"), (AGENT_RATE_LIMITED, "provider_rate_limited"),
               (AGENT_NOT_CONFIGURED, "model_not_configured"),
               (AGENT_NO_SUBMIT, "agent_no_submit"), (AGENT_TURN_CAP, "agent_turn_cap"),
               (AGENT_NO_TIME, "agent_no_time"), (PARSER_DEGRADED, "parser_degraded")]
    out: list[str] = []
    if obs.error:
        out.append("harness_timeout" if obs.error.startswith("harness_timeout") else "harness_error")
    for phase in obs.phases:
        r = phase.result
        if r is None:
            continue
        for text, cat in mapping:
            if text in r.degradations and cat not in out:
                out.append(cat)
        if any(d.startswith(AGENT_REPAIRS_EXHAUSTED.split("{")[0]) for d in r.degradations):
            out.append("repairs_exhausted")
        if r.status == "rejected" and (r.message or "").startswith(TOO_SLOW):
            out.append("run_deadline")
        elif r.status == "rejected" and (r.message or "").startswith(GENERIC_ERROR):
            out.append("run_crash")
        fin = finalize_data(phase.events)
        if fin.get("model") and fin.get("requested_model") and fin["model"] != fin["requested_model"]:
            out.append("provider_switch")
    return sorted(set(out))


def clarification_instances(case: EvalCase, phases: list[Phase]) -> list[dict[str, Any]]:
    out = []
    for i, ph in enumerate(phases):
        blocking = (i == 0) if case.clarification_answers else case.blocking
        out.append({"phase": i + 1, "blocking": blocking, "asked": ph.status == "needs_clarification"})
    return out


def node_ms(phases: list[Phase]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ph in phases:
        for e in ph.events:
            if e.type == "node_completed" and e.node and e.duration_ms is not None:
                out[e.node] = out.get(e.node, 0) + int(e.duration_ms)
    return out


def prefs_digest(prefs: Any) -> dict[str, Any] | None:
    """The parsed preferences as structured fields (drill-down for parse errors; no mood or free text)."""
    if prefs is None:
        return None
    return {"city": prefs.city, "budget": prefs.budget, "currency": prefs.currency, "start_min": prefs.start_min,
            "available_minutes": prefs.available_minutes, "energy": prefs.energy.value,
            "hard_constraints": sorted(c.key for c in prefs.hard_constraints), "interests": list(prefs.interests),
            "neighbourhood": prefs.neighbourhood}


def summed_usage(phases: list[Phase]) -> dict[str, Any]:
    from app.llm.usage import add_usage, empty_usage

    usage = empty_usage()
    for ph in phases:
        usage = add_usage(usage, finalize_data(ph.events).get("usage"))
    return usage


def build_record(sess: Session, obs: Observation, checks: list[Check], repeat: int,
                 judge: dict[str, Any] | None) -> dict[str, Any]:
    case, r = obs.case, obs.result
    fin = finalize_data(obs.events)
    http = {k: v for k, v in (obs.http or {}).items() if k not in {"bodies", "headers", "body"}} if obs.http else None
    if http is not None and obs.http:
        http["headers"] = {k: v for k, v in (obs.http.get("headers") or {}).items()
                           if k.lower() in {"content-type", "retry-after", "content-security-policy",
                                            "x-content-type-options"}}
        body = obs.http.get("body")
        http["detail"] = body.get("detail") if isinstance(body, dict) and "run_id" not in body else None
    return {
        "record_type": "run", "schema": RECORD_SCHEMA, "session": sess.session_for(obs.model), "suite": sess.suite,
        "case": case.id, "split": case.split, "category": case.category, "priority": case.priority,
        "repeat": repeat, "model": obs.model, "kind": obs.kind,
        "status": obs.status, "mode": r.mode if r else None, "result_model": r.model if r else None,
        "expected_statuses": list(case.expected_statuses), "status_ok": status_ok(obs),
        "feasible": case.feasible, "blocking": case.blocking,
        "hard_pass": hard_pass(checks) and not obs.error,
        "assertions": [c.as_dict() for c in checks],
        "finalize": {k: v for k, v in fin.items() if k != "usage"},
        "usage": summed_usage(obs.phases),
        "latency_ms": obs.latency_ms, "result_latency_ms": r.latency_ms if r else None, "deadline_s": obs.deadline_s,
        "phases": [{"phase": i + 1, "status": p.status, "mode": p.result.mode if p.result else None,
                    "latency_ms": p.latency_ms, "questions": [q.id for q in (p.result.questions if p.result else [])],
                    "resumed": p.answers is not None,
                    "finalize": {k: v for k, v in finalize_data(p.events).items() if k != "tool_calls"}}
                   for i, p in enumerate(obs.phases)],
        "clarification": clarification_instances(case, obs.phases),
        "parse": parse_fields(obs),
        "node_ms": node_ms(obs.phases),
        "degradations": list(r.degradations) if r else [],
        "error": obs.error,
        "error_categories": error_categories(obs),
        "judge": judge,
        "http": http,
        "plan": plan_digest(r),
        "preferences": prefs_digest(r.preferences if r else None),
        "message": (r.message or "")[:300] if r else None,
        "provider_calls": dict(obs.call_log.calls) if obs.call_log else {},
        "run_id": r.run_id if r else None,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        **sess.provenance,
    }


def _redact(text: str, secrets_: list[str]) -> str:
    """Secrets masked and local paths shortened (records are committed: no keys, no home directories)."""
    for secret in secrets_:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text.replace(f"{REPO_ROOT}{os.sep}", "").replace(str(Path.home()), "~")


def error_record(sess: Session, case: EvalCase, model_id: str, kind: str, repeat: int, exc: BaseException,
                 latency_ms: int, obs: Observation | None = None) -> dict[str, Any]:
    """The record of a run the harness could not finish: status "error", error category "harness_exception", no
    assertions, never a hard pass. Usage already spent by the planner is kept (it counts toward --max-cost-usd)."""
    from app.llm.usage import empty_usage

    usage = empty_usage()
    if obs is not None:
        with contextlib.suppress(Exception):
            usage = summed_usage(obs.phases)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {
        "record_type": "run", "schema": RECORD_SCHEMA, "session": sess.session_for(model_id), "suite": sess.suite,
        "case": case.id, "split": case.split, "category": case.category, "priority": case.priority,
        "repeat": repeat, "model": model_id, "kind": kind,
        "status": "error", "mode": None, "result_model": None,
        "expected_statuses": list(case.expected_statuses), "status_ok": False,
        "feasible": case.feasible, "blocking": case.blocking, "hard_pass": False,
        "assertions": [], "finalize": {}, "usage": usage,
        "latency_ms": latency_ms, "result_latency_ms": None, "deadline_s": obs.deadline_s if obs else None,
        "phases": [], "clarification": [], "parse": None, "node_ms": {}, "degradations": [],
        "error": _redact(f"{HARNESS_EXCEPTION}: {type(exc).__name__}: {exc}", sess.secrets)[:500],
        "error_categories": [HARNESS_EXCEPTION],
        "traceback": _redact(tb, sess.secrets)[-2000:],
        "judge": None, "http": None, "plan": None, "preferences": None, "message": None,
        "provider_calls": dict(obs.call_log.calls) if obs and obs.call_log else {},
        "run_id": None,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        **sess.provenance,
    }


async def run_one(sess: Session, case: EvalCase, model_id: str, kind: str, repeat: int) -> dict[str, Any]:
    """One run's record; a harness exception anywhere (setup, checks, judge, record) becomes an error_record."""
    t0 = time.perf_counter()
    obs: Observation | None = None
    try:
        data = request_data(case, sess.spec)
        if case.is_http and not sess.live:
            obs = await run_http_case(case, model_id, kind, data, sess)
        else:
            obs = await run_graph_case(case, model_id, kind, data, sess)
        checks = evaluate(obs)
        judge: dict[str, Any] | None = None
        if sess.judge is not None:
            judge, checks = await sess.judge.score(obs, checks)
        elif sess.judge_unavailable is not None:
            judge = dict(sess.judge_unavailable)
        return build_record(sess, obs, checks, repeat, judge)
    except Exception as exc:
        if sess.verbose:
            traceback.print_exc()
        return error_record(sess, case, model_id, kind, repeat, exc, _ms(t0), obs)


def record_cost_usd(rec: dict[str, Any]) -> float:
    """Estimated spend of one record (planner plus judge, registry pricing), for --max-cost-usd."""
    total = 0.0
    for usage in ((rec.get("usage") or {}), ((rec.get("judge") or {}).get("usage") or {})):
        with contextlib.suppress(TypeError, ValueError):
            total += float(usage.get("cost_usd") or 0.0)
    return total


def progress_line(i: int, total: int, rec: dict[str, Any], eta: str | None = None) -> str:
    ok = rec["hard_pass"]
    first = next((a for a in rec["assertions"] if a["kind"] == "code" and a["passed"] is False), None)
    why = f"  <- {first['name']}: {first['detail'][:90]}" if first else ""
    if rec.get("error"):
        why = f"  <- {rec['error'][:110]}"
    status = f"{rec['status']}/{rec['mode'] or '-'}"
    eta_text = f"  eta {eta}" if eta else ""
    return (f"[{i:>3}/{total}] {rec['model']:<24} {rec['case'] + '#' + str(rec['repeat']):<44} {status:<28} "
            f"{'PASS' if ok else 'FAIL'} {rec['latency_ms']:>7} ms{eta_text}{why}")


# ---------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------


def default_models(suite: str, spec: dict[str, Any]) -> list[str]:
    if suite == "offline":
        return ["fake:agent"]
    if suite == "live":
        from app.llm.registry import load_registry

        return [load_registry().production_primary]
    return [str(m["id"]) for m in spec.get("model_matrix") or []]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.runner", description=__doc__.split("\n\n")[0])
    p.add_argument("--suite", choices=SUITES, default="offline")
    p.add_argument("--models", default="", help="comma-separated registry IDs (default depends on the suite)")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--cases", default="", help="comma-separated case IDs (default: every case in the suite)")
    p.add_argument("--split", choices=SPLITS, default="all")
    p.add_argument("--judge-model", default=None,
                   help="registry ID of the judge model; default JUDGE_MODEL for the models and live suites; "
                        "'none' disables the judge")
    p.add_argument("--out", default=str(RESULTS_DIR), help="results directory (default: evals/results)")
    p.add_argument("--spec", default="", help="path to eval_cases.json (default: repo root)")
    gate = p.add_mutually_exclusive_group()
    gate.add_argument("--gate", dest="gate", action="store_true", default=None,
                      help="exit 1 if any run fails a hard check (default for the offline suite)")
    gate.add_argument("--no-gate", dest="gate", action="store_false")
    p.add_argument("--resume", action="store_true",
                   help="skip (case, repeat) runs already recorded in the latest session of each model in --out "
                        "(same record schema, git SHA, dirty-tree flag and hashes) and add new runs to that session")
    p.add_argument("--max-minutes", type=float, default=None,
                   help="wall-time budget per model for this invocation; remaining runs are left for --resume")
    p.add_argument("--max-cost-usd", type=float, default=None,
                   help="estimated-cost budget (planner + judge, registry pricing) for this whole invocation, checked "
                        "between runs; once exceeded the remaining runs are left for --resume")
    p.add_argument("--pace-s", type=float, default=0.0,
                   help="seconds to wait between runs (rate-limited free tiers, e.g. Cerebras 5 requests/minute); "
                        "the wait is not counted in run latency")
    p.add_argument("--verbose", action="store_true", help="show warnings from the app and harness tracebacks")
    args = p.parse_args(argv)
    if args.repeats < 1:
        p.error("--repeats must be at least 1")
    if args.max_minutes is not None and args.max_minutes < 0:
        p.error("--max-minutes must not be negative")
    if args.pace_s < 0:
        p.error("--pace-s must not be negative")
    if args.max_cost_usd is not None and not args.max_cost_usd >= 0:
        p.error("--max-cost-usd must not be negative")
    return args


def _configure_logging(verbose: bool) -> None:
    root = logging.getLogger()
    # Named like the app's JSON handler so app.observability.configure_logging (run by the API lifespan in HTTP
    # cases) sees it as already configured and does not add a JSON stdout handler that would interleave with the
    # progress lines.
    if not any(h.get_name() == "app-json" for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.set_name("app-json")
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.WARNING if verbose else logging.CRITICAL)


def make_session(args: argparse.Namespace, spec: dict[str, Any]) -> Session:
    sha, dirty = git_info(ignore=generated_paths(Path(args.out)))
    now = datetime.now(UTC)
    sess = Session(
        id=f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}", suite=args.suite, spec=spec, out_dir=Path(args.out),
        date=f"{now:%Y-%m-%d}", verbose=args.verbose,
    )
    env_values = env_settings_values()
    if sess.live:
        sess.extra = {}
    else:
        sess.extra = {**{k: v for k, v in env_values.items() if k != "langsmith_api_key"}, **SENTINEL_SETTINGS}
    found = [str(v) for k, v in {**env_values, **sess.extra}.items() if k in SECRET_FIELDS and v]
    found += [os.environ[n] for n in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
                                      "CEREBRAS_API_KEY",
                                      "LANGSMITH_API_KEY",
                                      "GOOGLE_MAPS_API_KEY", "TAVILY_API_KEY") if os.environ.get(n)]
    sess.secrets = sorted(set(found))
    sess.provenance = {
        "git_sha": sha, "git_dirty": dirty, "prompt_hash": prompt_hash(), "registry_hash": registry_hash(),
        "fixture_hash": fixture_hash(), "eval_cases_hash": spec_hash(args.spec or None),
        "harness_hash": harness_hash(),
    }
    return sess


def setup_judge(sess: Session, judge_model: str | None) -> None:
    if not judge_model or judge_model.strip().lower() in NO_JUDGE:
        return
    from app.deps import fixture_settings
    from evals.judge import RUBRIC_VERSION, Judge

    reason = unavailable_reason(judge_model, sess.extra)
    if reason:
        sess.judge_unavailable = {"status": "unavailable", "model": judge_model, "rubric_version": RUBRIC_VERSION,
                                  "reason": reason}
        print(f"judge {judge_model} unavailable: {reason}; judge_quality will be null", flush=True)
        return
    settings = fixture_settings(**{k: v for k, v in sess.extra.items() if k in {
        "anthropic_api_key", "google_api_key", "xai_api_key", "groq_api_key", "cerebras_api_key",
        "ollama_base_url"}})
    sess.judge = Judge(judge_model, settings)


def budget_exceeded(args: argparse.Namespace, elapsed_s: float, spent_usd: float) -> str | None:
    """Which budget stops the next run: "max_minutes" (this model's wall time is used up) or "max_cost_usd" (the
    estimated cost of this invocation exceeds the budget); None to continue. Checked between runs."""
    if args.max_minutes is not None and elapsed_s >= args.max_minutes * 60:
        return "max_minutes"
    if args.max_cost_usd is not None and spent_usd > args.max_cost_usd:
        return "max_cost_usd"
    return None


async def run(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec or None)
    cases = select_cases(load_cases(spec), args.suite, args.split,
                         [c.strip() for c in args.cases.split(",") if c.strip()] or None)
    models = [m.strip() for m in args.models.split(",") if m.strip()] or default_models(args.suite, spec)
    sess = make_session(args, spec)
    setup_judge(sess, args.judge_model)
    judge = args.judge_model if args.judge_model and args.judge_model.strip().lower() not in NO_JUDGE else "none"
    print(f"session {sess.id}: suite={args.suite} split={args.split} cases={len(cases)} models={models} "
          f"repeats={args.repeats} judge={judge} -> {Path(args.out) / sess.date}", flush=True)
    any_fail = False
    harness_failed = False
    stopped = False
    spent = 0.0  # estimated USD over every model in this invocation (--max-cost-usd)
    for model_id in models:
        kind = model_kind(model_id)
        reason = unavailable_reason(model_id, sess.extra)
        if reason:
            print(f"SKIP {model_id}: {reason}", flush=True)
            sess.write(model_id, {"record_type": "skip", "schema": RECORD_SCHEMA, "session": sess.id,
                                  "suite": sess.suite, "model": model_id, "kind": kind, "reason": reason,
                                  "timestamp": datetime.now(UTC).isoformat(timespec="seconds"), **sess.provenance})
            continue
        planned = [(case, repeat) for case in cases for repeat in range(1, args.repeats + 1)]
        if args.resume:
            point = resume_point(Path(args.out), sess.suite, model_id, sess.provenance)
            if point.session:
                sess.model_sessions[model_id] = point.session
            planned = [(c, r) for c, r in planned if (c.id, r) not in point.done]
            print(f"{model_id}: {point.note}; {len(planned)} run(s) to do", flush=True)
        total = len(planned)
        started = time.perf_counter()
        done = passes = 0
        for case, repeat in planned:
            elapsed = time.perf_counter() - started
            stop = budget_exceeded(args, elapsed, spent)
            if stop:
                remaining = total - done
                stopped = True
                sess.write(model_id, {"record_type": "budget_stop", "schema": RECORD_SCHEMA,
                                      "session": sess.session_for(model_id), "suite": sess.suite, "model": model_id,
                                      "kind": kind, "reason": stop, "max_minutes": args.max_minutes,
                                      "max_cost_usd": args.max_cost_usd, "spent_usd": round(spent, 6),
                                      "remaining": remaining,
                                      "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                                      **sess.provenance})
                used = (f"--max-minutes {args.max_minutes:g} used after {fmt_duration(elapsed)}"
                        if stop == "max_minutes" else
                        f"--max-cost-usd {args.max_cost_usd:g} exceeded (${spent:.4f} spent this invocation)")
                print(f"STOP {model_id}: {used}; {remaining} run(s) left (finish with --resume)", flush=True)
                break
            done += 1
            if args.pace_s and done > 1:
                await asyncio.sleep(args.pace_s)  # spacing for rate-limited providers; not part of run latency
            t0 = time.perf_counter()
            try:
                rec = await run_one(sess, case, model_id, kind, repeat)
            except Exception as exc:  # run_one records harness exceptions itself; this is the last resort
                if args.verbose:
                    traceback.print_exc()
                rec = error_record(sess, case, model_id, kind, repeat, exc, _ms(t0))
            sess.write(model_id, rec)
            harness_failed |= rec.get("status") == "error"
            spent += record_cost_usd(rec)
            passes += rec["hard_pass"]
            any_fail |= not rec["hard_pass"]
            per_run = (time.perf_counter() - started) / done
            eta = fmt_duration(per_run * (total - done)) if total > done else None
            print(progress_line(done, total, rec, eta), flush=True)
        print(f"== {model_id}: hard pass {passes}/{done} this invocation in "
              f"{fmt_duration(time.perf_counter() - started)} -> {sess.path_for(model_id)}", flush=True)
    if spent or args.max_cost_usd is not None:
        budget = f" of a ${args.max_cost_usd:g} budget" if args.max_cost_usd is not None else ""
        print(f"estimated cost this invocation: ${spent:.4f}{budget}", flush=True)
    gate = args.gate if args.gate is not None else args.suite == "offline"
    if harness_failed:
        return 2
    if gate and any_fail:
        return 1
    return 3 if stopped else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)
    env = load_env()  # backend/.env keys into os.environ, as make's `uv run --env-file` would
    args.judge_model = resolve_judge_model(args.judge_model, args.suite, env.judge_model if env else None)
    try:
        return asyncio.run(run(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\ninterrupted: finished runs are saved under {args.out}; continue with the same command plus --resume",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
