"""Tool-calling smoke test for a local Ollama model (stdlib only).

Sends one chat request with a single tool to Ollama's ``/api/chat`` and checks that the model answers with a
valid call to that tool: the right function name, a JSON object of arguments, every required argument present
with the right type, and no unknown arguments. Exit codes: 0 valid tool call, 1 no valid tool call,
2 Ollama unreachable, model not pulled or HTTP error.

    python scripts/ollama_smoke.py --model qwen3.5:9b
    python scripts/ollama_smoke.py --model granite4:tiny-h --base-url http://127.0.0.1:11434 --timeout 300

The prompt and tool are synthetic (no user data). The first call after Ollama starts also loads the model into
memory, so it can take much longer than later calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "ollama-smoke/1.0"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"

TOOL_NAME = "search_places"
TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Search for places to visit in a city. Returns candidate places with opening hours.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name, e.g. 'Bangalore'"},
                "category": {
                    "type": "string",
                    "enum": ["cafe", "restaurant", "park", "museum", "live_music"],
                    "description": "Kind of place to search for",
                },
                "max_results": {"type": "integer", "description": "How many places to return (1-10)"},
            },
            "required": ["city", "category"],
        },
    },
}
SYSTEM = "You plan outings. Use the provided tool to look up places; never invent them."
PROMPT = "Find up to 3 quiet cafes in Bangalore for this Saturday afternoon. Call the search tool."
JSON_TYPES: dict[str, type | tuple[type, ...]] = {"string": str, "integer": int, "number": (int, float),
                                                 "boolean": bool, "object": dict, "array": list}


def check_tool_call(message: dict[str, Any]) -> tuple[bool, str]:
    """(ok, reason) for the assistant message returned by /api/chat."""
    calls = message.get("tool_calls") or []
    if not calls:
        content = str(message.get("content") or "").strip().replace("\n", " ")
        return False, f"no tool call; the model replied with text: {content[:160]!r}"
    params = TOOL["function"]["parameters"]
    props: dict[str, Any] = params["properties"]
    parsed: list[dict[str, Any]] = []
    for call in calls:
        fn = call.get("function") or {}
        name = fn.get("name")
        if name != TOOL_NAME:
            return False, f"called unknown tool {name!r}"
        args = fn.get("arguments")
        if isinstance(args, str):  # some servers return a JSON string
            try:
                args = json.loads(args)
            except ValueError:
                return False, f"arguments are not valid JSON: {args[:160]!r}"
        if not isinstance(args, dict):
            return False, f"arguments are not an object: {args!r}"
        missing = [k for k in params["required"] if k not in args]
        if missing:
            return False, f"missing required arguments {missing}: {args}"
        unknown = [k for k in args if k not in props]
        if unknown:
            return False, f"unknown arguments {unknown}: {args}"
        for key, value in args.items():
            spec = props[key]
            expected = JSON_TYPES[spec["type"]]
            if spec["type"] == "integer" and isinstance(value, str) and value.strip().isdigit():
                value = int(value)  # tolerated: some models quote integers
            wrong_bool = isinstance(value, bool) and spec["type"] != "boolean"  # bool is an int subclass
            if wrong_bool or not isinstance(value, expected):
                return False, f"argument {key!r} has the wrong type: {value!r}"
            if "enum" in spec and value not in spec["enum"]:
                return False, f"argument {key!r} is not one of {spec['enum']}: {value!r}"
        if not str(args["city"]).strip():
            return False, "argument 'city' is empty"
        parsed.append(args)
    return True, f"{len(calls)} valid call(s), first: {TOOL_NAME}({json.dumps(parsed[0])})"


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data: dict[str, Any] = json.loads(resp.read().decode())
        return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tool-calling smoke test for a local Ollama model.")
    parser.add_argument("--model", required=True, help="Ollama model tag, e.g. qwen3.5:9b")
    parser.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds (includes model load)")
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--think", action="store_true",
                        help="leave thinking on (the app sends think=false, like this script by default)")
    args = parser.parse_args(argv)

    payload: dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT}],
        "tools": [TOOL],
        "stream": False,
        "options": {"temperature": 0, "num_ctx": args.num_ctx},
    }
    if not args.think:
        payload["think"] = False
    url = args.base_url.rstrip("/") + "/api/chat"
    started = time.perf_counter()
    try:
        data = post_json(url, payload, args.timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        print(f"FAIL {args.model}: HTTP {exc.code} from {url}: {detail}", file=sys.stderr)
        if exc.code == 404:
            print(f"     pull it first: ollama pull {args.model}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"FAIL {args.model}: cannot reach Ollama at {args.base_url}: {exc}", file=sys.stderr)
        return 2
    elapsed = time.perf_counter() - started
    load_s = (data.get("load_duration") or 0) / 1e9
    ok, reason = check_tool_call(data.get("message") or {})
    status = "ok  " if ok else "FAIL"
    print(f"{status} {args.model}: {reason} ({elapsed:.1f} s, model load {load_s:.1f} s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
