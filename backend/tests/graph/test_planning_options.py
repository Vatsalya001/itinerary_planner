import json

from app.domain.options import validated_option
from app.domain.schemas import Constraint, Evidence
from app.domain.validator import validate_plan
from app.graph.tools import SearchPlacesArgs, ToolEnv, compact_candidate, run_search_places
from tests.factories import make_candidate, make_ctx, make_prefs
from tests.graph.helpers import make_deps


def test_option_is_hydratable_and_meets_budget_time_and_required_interests():
    from app.domain.schemas import PlanDraft

    ctx = make_ctx(available=120)
    prefs = make_prefs(available=120, budget=1500, interests=["coffee", "culture"])
    pool = [make_candidate("fx:expensive", "cafe", cost=(1500, 1800)),
            make_candidate("fx:cafe", "cafe", cost=(200, 600)),
            make_candidate("fx:museum", "museum", cost=(20, 300), offset_km=(0.1, 0.1))]
    deps = make_deps()
    option = validated_option(pool, ctx, prefs, [], deps.travel)
    assert option is not None
    assert set(option["covered_interests"]) == {"coffee", "culture"}
    draft = PlanDraft.model_validate({"stops": option["stops"], "summary_note": option["summary_note"]})
    report = validate_plan(draft, {c.id: c for c in pool}, ctx, prefs, [], deps.travel)
    assert report.ok and report.plan is not None
    assert report.plan.totals.worst_case == option["worst_case_cost"] <= 1500
    assert report.plan.totals.elapsed_min == option["elapsed_minutes"] <= 120
    assert "fx:expensive" not in {s.candidate_id for s in draft.stops}


def test_option_does_not_relax_unknown_accessibility_or_claim_no_data_is_feasible():
    deps = make_deps()
    con = Constraint(kind="access", value="wheelchair")
    pool = [make_candidate("fx:cafe", "cafe", wheelchair=Evidence.UNKNOWN)]
    assert validated_option(pool, make_ctx(), make_prefs(), [con], deps.travel) is None
    assert validated_option([], make_ctx(), make_prefs(), [], deps.travel) is None


def test_compact_candidates_keep_evidence_and_arithmetic_but_remove_redundant_fields():
    c = make_candidate("fx:cafe", "cafe", diet={"vegetarian": Evidence.CONFIRMED, "vegan": Evidence.UNKNOWN})
    out = compact_candidate(c)
    assert out["diet"] == {"vegetarian": "confirmed"}
    assert out["wheelchair"] == "confirmed" and out["price"] and out["visit_minutes"] and out["hours_today"]
    assert out["effort"] == c.effort
    assert not {"source", "rating_count"} & set(out)
    assert len(json.dumps(out)) < len(json.dumps(c.model_dump(mode="json")))


async def test_search_attaches_checked_option_and_does_not_repeat_prior_records():
    deps = make_deps()
    env = ToolEnv(deps=deps, ctx=make_ctx(), prefs=make_prefs(), enforced=[], original=[], ledger={})
    args = SearchPlacesArgs.model_validate({"queries": [{"intent": "cafe", "category": "cafe"}]})
    first = await run_search_places(args, env)
    body = json.loads(first.content)
    assert body["validated_option"]["worst_case_cost"] <= env.prefs.budget
    env.ledger.update({c.id: c for c in first.candidates})
    second = await run_search_places(args, env)
    result = json.loads(second.content)["results"][0]
    assert result["candidates"] == [] and result["also"]
    assert second.candidates == []
