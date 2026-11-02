"""End to end with a local Ollama model on the frozen world. Needs Ollama running with the model pulled.

    RUN_OLLAMA=1 uv run pytest -m ollama tests/graph/test_ollama_e2e.py -s
    RUN_OLLAMA=1 OLLAMA_MODEL=ollama:qwen3.5:9b uv run pytest -m ollama tests/graph/test_ollama_e2e.py -s
"""

from __future__ import annotations

import os

import pytest

from tests.graph.helpers import (
    agent_tools,
    assert_plan_sound,
    assert_trace_integrity,
    collect,
    finalize_data,
    make_deps,
    request_for,
)

pytestmark = [
    pytest.mark.ollama,
    pytest.mark.skipif(os.environ.get("RUN_OLLAMA") != "1", reason="set RUN_OLLAMA=1 with Ollama running"),
]

LOCAL_DEADLINE_S = 240  # eval_cases.json deadlines_s.local


async def test_assignment_example_with_local_model() -> None:
    model_id = os.environ.get("OLLAMA_MODEL", "ollama:granite4:tiny-h")
    deps = make_deps(primary_model=model_id, run_deadline_s=LOCAL_DEADLINE_S,
                     ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    result, events = await collect(request_for("assignment_example"), deps)
    data = finalize_data(events)
    print(f"\n{model_id}: status={result.status} mode={result.mode} model={result.model} "
          f"latency_ms={result.latency_ms} turns={data['agent_turns']} tools={dict(agent_tools(events))} "
          f"submits={data['submit_attempts']} first_valid={data['first_submit_valid']} "
          f"protocol_errors={data['protocol_errors']} nudges={data['nudges']} usage={data['usage']} "
          f"degradations={result.degradations}")
    for e in events:
        if e.type in {"validation", "degraded"} or (e.type == "node_completed" and e.node == "plan_agent"):
            print(f"  {e.seq} {e.type} {e.node}: {e.summary} {e.data if e.type == 'validation' else ''}")
    assert_trace_integrity(events)
    assert result.status in {"complete", "fallback", "no_feasible_plan"}
    if result.status == "complete":
        assert result.mode == "agent"
    if result.plan is not None:
        assert_plan_sound(result)
