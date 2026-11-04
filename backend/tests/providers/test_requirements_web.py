"""Synthetic official pages and mocked Tavily HTTP; never uses live keys or APIs."""

import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.domain.requirements import evidence_satisfies, requirement_id
from app.domain.schemas import VenueRequirement
from app.providers.requirements_web import MAX_LOOKUPS, MAX_RESPONSE_BYTES, ResearchBudget, verify_candidates
from tests.factories import NOW, make_candidate, make_ctx, make_prefs
from tests.graph.helpers import make_deps

URL = "https://example.org/facilities"
BODY = "Synthetic Lotus Cafe in Bengaluru. A private changing room is available."


def setup_deps():
    deps = make_deps()
    deps.settings = deps.settings.model_copy(
        update={"tavily_api_key": SecretStr("synthetic-secret-not-a-real-key")}
    )
    p = make_prefs()
    p.venue_requirements = [
        VenueRequirement(
            id=requirement_id("private changing room", "all_stops"), description="private changing room"
        )
    ]
    c = make_candidate(name="Synthetic Lotus Cafe", website_url="https://example.org")
    return deps, p, c


async def test_official_raw_page_proof_cached_and_bounded():
    deps, p, c = setup_deps()
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.tavily.com/search"
        payload = json.loads(request.content)
        assert payload["include_domains"] == ["example.org"] and payload["include_raw_content"] is True
        return httpx.Response(200, json={"results": [{"url": URL, "raw_content": BODY}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deps.http = client
        budget = ResearchBudget()
        [verified] = await verify_candidates([c], p, make_ctx(), deps, budget)
        assert evidence_satisfies(verified, p.venue_requirements[0], "Bengaluru", NOW)
        [cached] = await verify_candidates([c], p, make_ctx(), deps, budget)
        assert cached.requirement_evidence == verified.requirement_evidence and len(calls) == budget.used == 1
        deps.clock = lambda: NOW + timedelta(hours=7)
        await verify_candidates([c], p, make_ctx(), deps, budget)
        assert len(calls) == 2


@pytest.mark.parametrize(
    "kind", ["429", "timeout", "invalid_json", "too_large", "snippet_only", "wrong_domain", "conflicting"]
)
async def test_provider_failures_and_search_snippets_never_confirm(kind):
    deps, p, c = setup_deps()

    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if kind == "429":
            return httpx.Response(429)
        if kind == "invalid_json":
            return httpx.Response(200, content=b"bad JSON")
        if kind == "too_large":
            return httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))
        item = {"url": URL, "raw_content": BODY}
        if kind == "snippet_only":
            item = {"url": URL, "content": BODY}
        if kind == "wrong_domain":
            item["url"] = "https://wrong.example.org"
        items = [item]
        if kind == "conflicting":
            items.append(
                {
                    "url": URL,
                    "raw_content": "Synthetic Lotus Cafe in Bengaluru. No private changing room is available.",
                }
            )
        return httpx.Response(200, json={"results": items})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deps.http = client
        [checked] = await verify_candidates([c], p, make_ctx(), deps, ResearchBudget())
    assert checked.requirement_evidence[0].status == "unknown"
    assert not evidence_satisfies(checked, p.venue_requirements[0], "Bengaluru", NOW)


async def test_lookup_cap_is_shared_and_normal_requests_make_no_calls():
    deps, p, c = setup_deps()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deps.http = client
        budget = ResearchBudget()
        first = [c.model_copy(update={"id": f"fx:{i}"}) for i in range(10)]
        result = await verify_candidates(first, p, make_ctx(), deps, budget)
        await verify_candidates(first, p, make_ctx(), deps, budget)
        assert len(calls) == budget.used == MAX_LOOKUPS
        assert all(x.requirement_evidence[0].status == "unknown" for x in result)
        await verify_candidates([c], make_prefs(), make_ctx(), deps, ResearchBudget())
        assert len(calls) == MAX_LOOKUPS


async def test_no_key_no_site_and_deadline_do_not_fetch_anything():
    deps, p, c = setup_deps()
    for budget in [ResearchBudget(deadline=0), ResearchBudget(spent=6), ResearchBudget(used=MAX_LOOKUPS)]:
        [checked] = await verify_candidates([c], p, make_ctx(), deps, budget)
        assert checked.requirement_evidence[0].status == "unknown"
    deps.settings = deps.settings.model_copy(update={"tavily_api_key": None})
    [checked] = await verify_candidates([c], p, make_ctx(), deps, ResearchBudget())
    assert checked.requirement_evidence[0].status == "unknown"
