"""Bounded facility research via Tavily raw official pages; no direct arbitrary-URL fetching."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.deps import Deps
from app.domain.requirements import EVIDENCE_TTL, applies, assess_text, official_host
from app.domain.schemas import (
    Candidate,
    Preferences,
    VenueRequirement,
    VenueRequirementEvidence,
    WorldContext,
)

MAX_LOOKUPS = 6
MAX_RESPONSE_BYTES = 150_000
MAX_CACHE_ENTRIES = 128
TAVILY_URL = "https://api.tavily.com/search"


@dataclass
class ResearchBudget:
    used: int = 0
    spent: float = 0
    deadline: float = float("inf")


async def verify_candidates(
    candidates: list[Candidate], prefs: Preferences, ctx: WorldContext, deps: Deps, budget: ResearchBudget
) -> list[Candidate]:
    if not prefs.venue_requirements:
        return candidates
    out = []
    cache: dict[Any, Any] = deps.extras.setdefault("facility_evidence_cache", {})
    for candidate in candidates:
        requirements = [r for r in prefs.venue_requirements if applies(r, candidate)]
        if not requirements:
            out.append(candidate)
            continue
        now = deps.now()
        key = (candidate.id, candidate.website_url, ctx.location.id, tuple(r.id for r in requirements))
        previous = cache.get(key)
        if previous and previous[0] <= now < previous[0] + EVIDENCE_TTL:
            out.append(candidate.model_copy(update={"requirement_evidence": previous[1]}))
            continue
        evidence = [
            VenueRequirementEvidence(
                requirement_id=r.id,
                description=r.description,
                reason="Research unavailable, exhausted or no official website",
            )
            for r in requirements
        ]
        host = official_host(candidate.website_url)
        if host and deps.http and deps.settings.tavily_api_key and budget.used < MAX_LOOKUPS:
            timeout = min(2.5, 6.0 - budget.spent, budget.deadline - time.perf_counter())
            if timeout > 0.1:
                budget.used += 1
                started = time.perf_counter()
                try:

                    async def lookup(
                        candidate: Candidate = candidate,
                        requirements: list[VenueRequirement] = requirements,
                        host: str = host,
                        timeout: float = timeout,
                    ) -> list[dict[str, Any]]:
                        assert deps.http is not None and deps.settings.tavily_api_key is not None
                        async with deps.http.stream(
                            "POST",
                            TAVILY_URL,
                            headers={
                                "Authorization": f"Bearer {deps.settings.tavily_api_key.get_secret_value()}"
                            },
                            json={
                                "query": f"{candidate.name} {ctx.location.name} facilities "
                                + " ".join(r.description for r in requirements),
                                "include_domains": [host],
                                "search_depth": "basic",
                                "max_results": 3,
                                "include_answer": False,
                                "include_raw_content": True,
                            },
                            timeout=httpx.Timeout(timeout),
                            follow_redirects=False,
                        ) as response:
                            if response.status_code != 200:
                                return []
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > MAX_RESPONSE_BYTES:
                                    return []
                            data = json.loads(body)
                            results = data.get("results", []) if isinstance(data, dict) else []
                            return results[:3] if isinstance(results, list) else []

                    results = await asyncio.wait_for(lookup(), timeout)
                    evidence = []
                    for r in requirements:
                        findings = [
                            assess_text(
                                candidate, r, item["url"], item["raw_content"], ctx.location.name, now
                            )
                            for item in results
                            if isinstance(item, dict)
                            and isinstance(item.get("url"), str)
                            and isinstance(item.get("raw_content"), str)
                        ]
                        positive = [e for e in findings if e.status == "confirmed"]
                        negative = [e for e in findings if e.status == "contradicted"]
                        if positive and negative:
                            evidence.append(
                                VenueRequirementEvidence(
                                    requirement_id=r.id,
                                    description=r.description,
                                    reason="Conflicting official-source statements",
                                )
                            )
                        else:
                            evidence.append(
                                (
                                    negative
                                    or positive
                                    or findings
                                    or [
                                        VenueRequirementEvidence(
                                            requirement_id=r.id,
                                            description=r.description,
                                            reason="No explicit official-source evidence found",
                                        )
                                    ]
                                )[0]
                            )
                except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
                    pass  # uncertainty stays explicit; no retries or raw exceptions containing keys
                finally:
                    budget.spent += time.perf_counter() - started
        if evidence and all(e.status != "unknown" for e in evidence):
            if len(cache) >= MAX_CACHE_ENTRIES:
                cache.pop(next(iter(cache)))
            cache[key] = (now, evidence)
        out.append(candidate.model_copy(update={"requirement_evidence": evidence}))
    return out
