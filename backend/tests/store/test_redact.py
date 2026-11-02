"""redact.for_storage / redact.rehydrate (Google Maps terms and the no-free-text rule)."""

from __future__ import annotations

import json
from typing import Any

from app.domain.schemas import PlanResult
from app.store import redact
from tests.api.plan_factories import (
    CONSTRAINT_TEXT,
    GOOGLE_ADDRESS,
    GOOGLE_ID,
    GOOGLE_NAME,
    GOOGLE_PLAN_B_ID,
    GOOGLE_PLAN_B_NAME,
    MOOD,
    NOW,
    SNAP_ID,
    FakePlaces,
    make_result,
)


def test_for_storage_strips_free_text_and_google_display_fields() -> None:
    result = make_result().model_copy(update={"share_id": "share_1", "thread_id": "t_abcdef12"})
    record = redact.for_storage(result)
    raw = json.dumps(record, ensure_ascii=False)

    for forbidden in (MOOD, CONSTRAINT_TEXT, "somewhere quiet", GOOGLE_NAME, GOOGLE_PLAN_B_NAME, GOOGLE_ADDRESS,
                      "Ashok Nagar", "maps.google.com"):
        assert forbidden not in raw, forbidden
    stored = record["result"]
    assert record["v"] == redact.RECORD_VERSION
    assert record["redacted"] == {"google_candidates": [GOOGLE_ID, GOOGLE_PLAN_B_ID]}
    assert stored["thread_id"] is None and stored["share_id"] == "share_1"
    assert "mood" not in stored["preferences"] and "soft_preferences" not in stored["preferences"]
    assert stored["preferences"]["hard_constraints"] == [{"kind": "diet", "value": "vegetarian"}]
    assert stored["preferences"]["interests"] == ["food", "walks"]

    cafe = stored["plan"]["stops"][0]["candidate"]
    assert cafe["id"] == GOOGLE_ID and cafe["name"] == ""
    for key in ("address", "neighbourhood", "rating", "rating_count", "maps_url"):
        assert cafe[key] is None, key
    assert cafe["hours"] == {"status": "unknown", "periods": []}
    assert cafe["source"]["url"] is None and cafe["source"]["provider"] == "google_places"
    # computed fields kept
    assert cafe["cost"] == {"min": 200, "max": 400, "currency": "INR", "basis": "price_level_estimate"}
    assert (cafe["lat"], cafe["lng"], cafe["category"], cafe["city_id"]) == (12.9716, 77.5946, "cafe",
                                                                            "fixture:bangalore")
    assert cafe["diet"] == {"vegetarian": "confirmed"} and cafe["seated"] is True
    assert cafe["duration_range"] == [30, 60, 90]
    # our prose that named the place is tokenised
    assert stored["plan"]["stops"][0]["why"].startswith(f"[[place:{GOOGLE_ID}]]")
    assert stored["checks"][0]["detail"].startswith(f"[[place:{GOOGLE_ID}]]")

    park = stored["plan"]["stops"][1]["candidate"]
    assert park == make_result().plan.stops[1].candidate.model_dump(mode="json")  # type: ignore[union-attr]
    # the stored record is still a schema-valid PlanResult
    PlanResult.model_validate(stored)


def test_for_storage_without_plan_or_google() -> None:
    record = redact.for_storage(make_result(status="needs_clarification"))
    assert record["redacted"] == {"google_candidates": []}
    assert MOOD not in json.dumps(record)


async def test_rehydrate_restores_names_and_prose() -> None:
    places = FakePlaces()
    original = make_result()
    restored = await redact.rehydrate(redact.for_storage(original), places, now=NOW)
    assert restored.plan is not None and original.plan is not None
    cafe = restored.plan.stops[0].candidate
    assert cafe.name == GOOGLE_NAME and cafe.address == "Fresh address from Places" and cafe.rating == 4.6
    assert cafe.cost == original.plan.stops[0].candidate.cost
    assert restored.plan.stops[0].plan_b is not None and restored.plan.stops[0].plan_b.name == GOOGLE_PLAN_B_NAME
    assert restored.plan.stops[0].why == original.plan.stops[0].why
    assert restored.checks == original.checks
    assert restored.plan.stops[1] == original.plan.stops[1]
    assert redact.UNAVAILABLE_WARNING not in restored.warnings
    assert {cid for cid, _ in places.get_calls} == {GOOGLE_ID, GOOGLE_PLAN_B_ID}


async def test_rehydrate_restores_hours_and_rating_but_never_our_computed_fields() -> None:
    from app.domain.schemas import CostRange, Hours

    class Drifted(FakePlaces):
        """Fresh details whose category, cost and other computed fields differ from what was planned."""

        async def get(self, candidate_id: str, ctx: Any) -> Any:
            fresh = await super().get(candidate_id, ctx)
            return fresh.model_copy(update={
                "category": "event", "cost": CostRange(min=9000, max=9999, currency="INR", basis="listed_price_range"),
                "duration_range": (5, 5, 5), "tags": ["drifted"], "seated": False,
                "hours": Hours(status="known", periods=[(600, 1200)]), "rating": 3.9, "rating_count": 77,
            })

    original = make_result()
    restored = await redact.rehydrate(redact.for_storage(original), Drifted(), now=NOW)
    assert restored.plan is not None and original.plan is not None
    cafe, stored = restored.plan.stops[0].candidate, original.plan.stops[0].candidate
    # display fields come from the fresh details (hours and rating included)
    assert (cafe.hours.periods, cafe.rating, cafe.rating_count) == ([(600, 1200)], 3.9, 77)
    # what we computed and validated is kept exactly
    for key in ("category", "cost", "duration_range", "tags", "seated", "diet", "lat", "lng", "city_id"):
        assert getattr(cafe, key) == getattr(stored, key), key
    assert restored.plan.stops[0].start_min == original.plan.stops[0].start_min
    assert restored.plan.totals == original.plan.totals


async def test_rehydrate_through_google_places_details() -> None:
    import httpx
    import respx

    from app.providers.google_places import DETAILS_FIELD_MASK, GooglePlaces

    def details(pid: str, name: str, types: list[str]) -> dict[str, Any]:
        return {
            "id": pid, "displayName": {"text": name}, "formattedAddress": "Synthetic address",
            "location": {"latitude": 12.9716, "longitude": 77.5946}, "types": types,
            "googleMapsUri": f"https://maps.google.com/?cid={pid}", "businessStatus": "OPERATIONAL",
            "rating": 4.7, "userRatingCount": 2100,
            "regularOpeningHours": {"periods": [{"open": {"day": 6, "hour": 8, "minute": 0},
                                                 "close": {"day": 6, "hour": 22, "minute": 0}}]},
        }

    cafe_pid, plan_b_pid = GOOGLE_ID.removeprefix("gp:"), GOOGLE_PLAN_B_ID.removeprefix("gp:")
    with respx.mock(assert_all_called=True) as mock:
        cafe_route = mock.get(f"https://places.googleapis.com/v1/places/{cafe_pid}").mock(
            return_value=httpx.Response(200, json=details(cafe_pid, GOOGLE_NAME, ["cafe"])))
        # types that map to none of our categories: the place is still shown, with its stored category
        mock.get(f"https://places.googleapis.com/v1/places/{plan_b_pid}").mock(
            return_value=httpx.Response(200, json=details(plan_b_pid, GOOGLE_PLAN_B_NAME, ["point_of_interest"])))
        async with httpx.AsyncClient() as client:
            restored = await redact.rehydrate(redact.for_storage(make_result()), GooglePlaces("k", client), now=NOW)
    assert cafe_route.calls.last.request.headers["X-Goog-FieldMask"] == DETAILS_FIELD_MASK
    assert restored.plan is not None
    cafe, plan_b = restored.plan.stops[0].candidate, restored.plan.stops[0].plan_b
    assert cafe.name == GOOGLE_NAME and cafe.rating == 4.7 and cafe.rating_count == 2100
    assert cafe.hours.status == "known" and cafe.hours.periods == [(480, 1320)]
    assert plan_b is not None and plan_b.name == GOOGLE_PLAN_B_NAME and plan_b.category == "cafe"
    assert redact.UNAVAILABLE_WARNING not in restored.warnings


async def test_rehydrate_failure_and_no_provider() -> None:
    record = redact.for_storage(make_result())
    for places in (FakePlaces(fail=True), None):
        restored = await redact.rehydrate(record, places, now=NOW)
        assert restored.plan is not None
        cafe = restored.plan.stops[0].candidate
        assert cafe.name == redact.UNAVAILABLE_NAME
        assert cafe.maps_url == redact.google_place_url(GOOGLE_ID)
        assert "query_place_id=ChIJquietleaf0001" in (cafe.maps_url or "")
        assert restored.plan.stops[0].why.startswith("this place")
        assert redact.UNAVAILABLE_WARNING in restored.warnings
        assert restored.plan.stops[1].candidate.id == SNAP_ID


async def test_rehydrate_slow_provider_times_out() -> None:
    import asyncio

    class Slow(FakePlaces):
        async def get(self, candidate_id: str, ctx: Any) -> Any:
            await asyncio.sleep(5)

    restored = await redact.rehydrate(redact.for_storage(make_result()), Slow(), now=NOW, timeout_s=0.01)
    assert restored.plan is not None and restored.plan.stops[0].candidate.name == redact.UNAVAILABLE_NAME


async def test_rehydrate_accepts_bare_result_record() -> None:
    raw = make_result().model_dump(mode="json")
    restored = await redact.rehydrate(raw, None, now=NOW)
    assert restored == make_result()
