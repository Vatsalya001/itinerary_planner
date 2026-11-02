"""Eval cases: load eval_cases.json, apply its merge semantics, build per-case settings and deps.

Merge semantics (eval_cases.json ``merge_semantics``): shallow-merge ``input_overrides`` into ``defaults``
(arrays replace defaults), then delete ``remove_fields``; ``free_text`` is sent as conversational input; with
``use_defaults: false`` only ``free_text`` is sent (``clarification_answers`` are sent on resume).

Every run gets its own deps: the frozen world (fixture places, events, weather, geocoder, fixed clock), the case's
event scenarios, fault and config overrides, a fresh in-memory checkpointer, and recording proxies around the
providers so assertions can check that every stop was returned by a provider *in this run* without trusting the
planner's ledger.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from evals import BACKEND_DIR, EVAL_CASES_PATH

if TYPE_CHECKING:
    from app.config import Settings
    from app.deps import Deps

SUITES = ("offline", "models", "live")
SPLITS = ("dev", "holdout", "all")
# Cases that exercise the HTTP layer (status codes, body limits, rate limits, share links); run via create_app.
HTTP_CASES = frozenset({"negative_budget", "oversize_body", "rate_limited", "xss_in_input", "share_link_roundtrip"})
# Faults the harness simulates itself (they are not app faults, so they never reach settings.fault).
HARNESS_FAULTS = frozenset({"exceed_per_ip_limit"})
_SIZE_PLACEHOLDER = re.compile(r"^<(\d+)\s*KB string>$", re.IGNORECASE)


@dataclass(frozen=True)
class EvalCase:
    id: str
    priority: str
    split: str
    category: str
    suites: tuple[str, ...]
    expected_statuses: tuple[str, ...]
    assertions: tuple[str, ...]
    input_overrides: dict[str, Any] = field(default_factory=dict)
    use_defaults: bool = True
    free_text: str | None = None
    remove_fields: tuple[str, ...] = ()
    clarification_answers: dict[str, str] | None = None
    parse_expect: dict[str, Any] | None = None
    fixtures: dict[str, Any] = field(default_factory=dict)
    fault: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def is_http(self) -> bool:
        return self.id in HTTP_CASES

    @property
    def blocking(self) -> bool:
        """A blocking case must stop for a clarification (expected_statuses is exactly needs_clarification)."""
        return self.expected_statuses == ("needs_clarification",)

    @property
    def feasible(self) -> bool:
        """A plan is required: complete is expected and no_feasible_plan is not an acceptable outcome."""
        return "complete" in self.expected_statuses and "no_feasible_plan" not in self.expected_statuses

    @property
    def weather(self) -> str:
        return str(self.fixtures.get("weather", "clear"))

    @property
    def event_scenarios(self) -> list[str] | None:
        events = self.fixtures.get("events")
        return list(events) if events is not None else None

    @property
    def app_fault(self) -> str | None:
        return self.fault if self.fault and self.fault not in HARNESS_FAULTS else None


@lru_cache(maxsize=4)
def _load_spec_cached(path: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return data


def load_spec(path: str | Path | None = None) -> dict[str, Any]:
    """The parsed eval_cases.json (a deep copy, so callers may mutate it)."""
    return copy.deepcopy(_load_spec_cached(str(path or EVAL_CASES_PATH)))


def parse_case(raw: dict[str, Any]) -> EvalCase:
    return EvalCase(
        id=str(raw["id"]),
        priority=str(raw.get("priority", "")),
        split=str(raw.get("split", "dev")),
        category=str(raw.get("category", "uncategorized")),
        suites=tuple(raw.get("suites") or ()),
        expected_statuses=tuple(raw.get("expected_statuses") or ()),
        assertions=tuple(raw.get("assertions") or ()),
        input_overrides=copy.deepcopy(raw.get("input_overrides") or {}),
        use_defaults=bool(raw.get("use_defaults", True)),
        free_text=raw.get("free_text"),
        remove_fields=tuple(raw.get("remove_fields") or ()),
        clarification_answers=dict(raw["clarification_answers"]) if raw.get("clarification_answers") else None,
        parse_expect=copy.deepcopy(raw.get("parse_expect")),
        fixtures=copy.deepcopy(raw.get("fixtures") or {}),
        fault=raw.get("fault"),
        config=copy.deepcopy(raw.get("config") or {}),
    )


def load_cases(spec: dict[str, Any] | None = None) -> list[EvalCase]:
    spec = spec if spec is not None else load_spec()
    return [parse_case(c) for c in spec["cases"]]


def select_cases(cases: list[EvalCase], suite: str, split: str = "all", ids: list[str] | None = None) -> list[EvalCase]:
    """Cases in ``suite`` (and ``split`` unless "all"), optionally limited to ``ids`` (unknown IDs raise)."""
    if suite not in SUITES:
        raise ValueError(f"unknown suite {suite!r}; expected one of {SUITES}")
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}; expected one of {SPLITS}")
    chosen = [c for c in cases if suite in c.suites and (split == "all" or c.split == split)]
    if ids:
        known = {c.id for c in cases}
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ValueError(f"unknown case id(s): {', '.join(unknown)}")
        wanted = set(ids)
        chosen = [c for c in chosen if c.id in wanted]
    return chosen


def expand_placeholder(value: Any) -> Any:
    """``"<20 KB string>"`` becomes a 20 KiB string (the oversize_body case); everything else is unchanged."""
    if isinstance(value, str):
        m = _SIZE_PLACEHOLDER.match(value.strip())
        if m:
            return "x" * (int(m.group(1)) * 1024)
    return value


def request_data(case: EvalCase, spec: dict[str, Any]) -> dict[str, Any]:
    """The request body for a case, per eval_cases.json merge_semantics."""
    if case.use_defaults:
        data: dict[str, Any] = copy.deepcopy(spec.get("defaults") or {})
        for key, value in case.input_overrides.items():
            data[key] = copy.deepcopy(value)  # shallow merge: arrays replace, they are not concatenated
        for key in case.remove_fields:
            data.pop(key, None)
    else:
        data = {}
    if case.free_text:
        data["free_text"] = case.free_text
    return {k: expand_placeholder(v) for k, v in data.items()}


def fixed_now(spec: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(spec["fixed_context"]["now"])


def target_date(spec: dict[str, Any]) -> date:
    return date.fromisoformat(spec["fixed_context"]["target_saturday"])


def default_start_min(spec: dict[str, Any]) -> int:
    hh, mm = str(spec["fixed_context"].get("default_start_time", "15:00")).split(":")
    return int(hh) * 60 + int(mm)


# ---------------------------------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------------------------------


def model_kind(model_id: str) -> str:
    """Registry kind (baseline | api | local | fake), "unknown" when the provider isn't in the registry."""
    from app.llm.registry import find_spec, load_registry

    spec = find_spec(model_id)
    if spec is not None:
        return spec.kind
    provider = model_id.partition(":")[0]
    for entry in load_registry().specs.values():
        if entry.provider == provider:
            return entry.kind
    return "unknown"


def deadline_s(kind: str, spec: dict[str, Any]) -> int:
    """Run deadline from eval_cases.json deadlines_s: local rows get the long one, everything else the API one."""
    deadlines = spec.get("deadlines_s") or {}
    return int(deadlines.get("local", 240) if kind == "local" else deadlines.get("api", 60))


# ---------------------------------------------------------------------------------------------------
# Settings and deps
# ---------------------------------------------------------------------------------------------------


def settings_overrides(case: EvalCase, model_id: str, spec: dict[str, Any],
                       extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keyword overrides for app.deps.fixture_settings for one case and model."""
    from app.config import Settings

    overrides: dict[str, Any] = {
        "primary_model": model_id,
        "fallback_model": None,
        "fault": case.app_fault,
        "fixture_weather": case.weather,
        "fixed_now": fixed_now(spec),
        "run_deadline_s": deadline_s(model_kind(model_id), spec),
    }
    for key, value in case.config.items():
        name = key.lower()
        if name not in Settings.model_fields:
            raise ValueError(f"case {case.id}: config key {key!r} is not a setting")
        overrides[name] = value
    overrides.update(extra or {})
    return overrides


def case_settings(case: EvalCase, model_id: str, spec: dict[str, Any], extra: dict[str, Any] | None = None) -> Settings:
    from app.deps import fixture_settings

    return fixture_settings(**settings_overrides(case, model_id, spec, extra))


class CallLog:
    """What the providers returned and how often they were called during one run."""

    def __init__(self) -> None:
        self.ids: set[str] = set()  # candidate IDs any provider returned (stops and Plan Bs must be among them)
        self.calls: Counter[str] = Counter()

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())


class RecordingProvider:
    """Transparent proxy: forwards everything, counts async provider calls, records returned candidate IDs."""

    _METHODS = frozenset({"search", "get", "resolve", "forecast"})

    def __init__(self, inner: Any, label: str, log: CallLog) -> None:
        self._inner = inner
        self._label = label
        self._log = log

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name not in self._METHODS or not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            self._log.calls[f"{self._label}.{name}"] += 1
            out = await attr(*args, **kwargs)
            items = out if isinstance(out, list) else [out]
            for item in items:
                cid = getattr(item, "id", None)
                if isinstance(cid, str) and hasattr(item, "category"):
                    self._log.ids.add(cid)
            return out

        return call


def install_recorders(deps: Deps) -> CallLog:
    log = CallLog()
    deps.places = cast(Any, RecordingProvider(deps.places, "places", log))
    if deps.snapshot is not None:
        deps.snapshot = cast(Any, RecordingProvider(deps.snapshot, "snapshot", log))
    if deps.events is not None:
        deps.events = cast(Any, RecordingProvider(deps.events, "events", log))
    deps.geocoder = cast(Any, RecordingProvider(deps.geocoder, "geocoder", log))
    deps.weather = cast(Any, RecordingProvider(deps.weather, "weather", log))
    return log


def build_case_deps(case: EvalCase, settings: Settings) -> tuple[Deps, CallLog]:
    """Frozen-world deps for one run: case event scenarios, fresh checkpointer, recording providers."""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.deps import build_deps
    from app.providers.fixture import FixtureEvents

    deps = build_deps(settings)
    scenarios = case.event_scenarios
    if scenarios is not None and settings.resolved_events_provider() == "fixture":
        events: Any = FixtureEvents(scenarios)
        if case.app_fault == "events_search_error":
            from app.providers.faults import FailingEvents

            events = FailingEvents(events)
        deps.events = events
    deps.checkpointer = InMemorySaver()
    return deps, install_recorders(deps)


# ---------------------------------------------------------------------------------------------------
# Provenance hashes
# ---------------------------------------------------------------------------------------------------


def _digest(paths: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(str(path.relative_to(root)).encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


# Source files that hold text the planner model sees: prompts, payload formats and the LLMParse structured-output
# schema (prompts.py); tool names, descriptions, JSON schemas and tool result/error texts (tools.py); tool-call
# error texts (nodes/execute.py); the repair instruction and submit_plan replies (nodes/validate.py).
MODEL_VISIBLE_FILES = ("app/graph/prompts.py", "app/graph/tools.py", "app/graph/nodes/execute.py",
                       "app/graph/nodes/validate.py")


def model_visible_text() -> str:
    """The static text as rendered for the model: system prompts, nudge, tool schemas and the LLMParse JSON schema.

    Rendering catches text those files pull from elsewhere (the constraint vocabulary in the parse prompt, the
    category list in the search_places schema)."""
    from app.graph.prompts import AGENT_SYSTEM_PROMPT, NUDGE_TEXT, PARSE_SYSTEM_PROMPT, LLMParse
    from app.graph.tools import tool_schemas

    return json.dumps({"agent_system": AGENT_SYSTEM_PROMPT, "parse_system": PARSE_SYSTEM_PROMPT, "nudge": NUDGE_TEXT,
                       "tools": tool_schemas(), "parse_schema": LLMParse.model_json_schema()},
                      sort_keys=True, ensure_ascii=False)


def prompt_hash() -> str:
    """Hash of everything the planner model is shown: the MODEL_VISIBLE_FILES plus model_visible_text().

    Dynamic tool results built by domain code (validator violation messages, candidate fields) are covered by the
    git SHA only."""
    h = hashlib.sha256()
    h.update(_digest([BACKEND_DIR / f for f in MODEL_VISIBLE_FILES], BACKEND_DIR).encode("utf-8"))
    h.update(b"\0")
    h.update(model_visible_text().encode("utf-8"))
    return h.hexdigest()[:12]


def registry_hash() -> str:
    return _digest([BACKEND_DIR / "app" / "llm" / "models.yaml"], BACKEND_DIR)


def fixture_hash() -> str:
    """Hash of the frozen world: fixtures, the curated snapshot and the reference tables in data/."""
    data = BACKEND_DIR / "data"
    files = [p for p in data.rglob("*") if p.is_file() and p.suffix in {".json", ".yaml", ".yml"}
             and not p.name.startswith("app.db") and not p.name.startswith("checkpoints")]
    return _digest(files, BACKEND_DIR)


def harness_hash() -> str:
    """Hash of the eval harness code (assertions decide pass/fail, so a change makes old records incomparable)."""
    here = BACKEND_DIR / "evals"
    return _digest([p for p in here.glob("*.py")], BACKEND_DIR)


def spec_hash(path: str | Path | None = None) -> str:
    p = Path(path or EVAL_CASES_PATH)
    return _digest([p], p.parent)
