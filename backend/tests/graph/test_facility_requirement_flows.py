"""Requirements cannot disappear across the human clarification boundary or backup planner."""

import pytest

from app.domain.schemas import PlanRequest
from tests.graph.helpers import collect, make_deps


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("Every venue must have a teleportation chamber", "no_feasible_plan"),
        ("Keep it as a preference", "complete"),
        ("Skip it", "complete"),
    ],
)
async def test_unknown_requirement_after_human_answer(answer, expected):
    deps = make_deps()
    request = PlanRequest(
        city="Mumbai",
        budget=3000,
        available_time=240,
        interests=["food", "museums"],
        constraints=["Every venue must have a teleportation chamber"],
    )
    paused, _ = await collect(request, deps, model_id="baseline:deterministic")
    assert paused.status == "needs_clarification" and paused.thread_id
    result, events = await collect(
        None, deps, thread_id=paused.thread_id, resume_answers={"constraint": answer}
    )
    assert result.status == expected
    if expected == "no_feasible_plan":
        assert result.plan is None and "cannot verify" in result.message
        assert not any(e.tool == "search_places" for e in events)
    if answer == "Keep it as a preference":
        assert any("teleportation" in s for s in result.preferences.soft_preferences)


@pytest.mark.parametrize("model", ["baseline:deterministic", "fake:agent"])
async def test_mandatory_known_facility_without_evidence_has_no_plan(model):
    deps = make_deps()
    request = PlanRequest(
        city="Mumbai",
        budget=3000,
        available_time=240,
        interests=["food"],
        constraints=["Every venue must have a private changing room"],
    )
    result, _ = await collect(request, deps, model_id=model)
    assert result.status == "no_feasible_plan" and result.plan is None
    assert result.infeasible and "Missing evidence" in result.infeasible.reason
    [relaxation] = result.infeasible.relaxations
    relaxed = request.model_copy(update=relaxation.patch)
    accepted, _ = await collect(relaxed, make_deps(), model_id="baseline:deterministic")
    assert accepted.status == "complete" and not accepted.preferences.venue_requirements[0].mandatory
    assert next(c for c in accepted.checks if c.name == "venue_requirements").status == "unverified"


async def test_official_proof_flows_into_complete_plan_without_real_api():
    import httpx
    from pydantic import SecretStr

    from tests.factories import make_candidate

    deps = make_deps()
    deps.settings = deps.settings.model_copy(update={"tavily_api_key": SecretStr("synthetic-test-key")})
    [loc] = await deps.geocoder.resolve("Mumbai")
    candidate = make_candidate(
        id="fx:synthetic_mumbai_cafe",
        name="Synthetic Lotus Cafe",
        lat=loc.lat,
        lng=loc.lng,
        city_id=loc.id,
        website_url="https://example.org",
        cost=100,
    )

    class SyntheticPlaces:
        name = "fixture"

        async def search(self, query, ctx):
            return [candidate]

    deps.places = SyntheticPlaces()

    def handler(request):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.org/facilities",
                        "raw_content": "Synthetic Lotus Cafe in Mumbai. A private changing room is available.",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deps.http = client
        result, _ = await collect(
            PlanRequest(
                city="Mumbai",
                budget=1000,
                available_time=120,
                interests=["food"],
                constraints=["Every venue must have a private changing room"],
            ),
            deps,
            model_id="baseline:deterministic",
        )
    assert result.status == "complete" and result.plan
    assert result.plan.stops[0].candidate.requirement_evidence[0].status == "confirmed"
    assert next(c for c in result.checks if c.name == "venue_requirements").status == "pass"
