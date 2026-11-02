"""Deterministic travel estimates between candidates (haversine x detour / per-city mode speeds).

OWNER: domain agent (Phase 1). Reads data/cities.yaml (speeds, pickup minutes, fare tables per city
with source and date; a "default" entry for unknown cities). Contract only.
"""

from __future__ import annotations

from app.domain.schemas import Candidate, Leg, WorldContext


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    raise NotImplementedError


class HeuristicTravel:
    """Satisfies app.providers.base.TravelEstimator.

    Rules: distance = haversine x 1.4. Walk if <= 1.2 km (4.5 km/h). Otherwise auto (cab when the city
    has no autos) at the city's speed plus pickup minutes. minutes_max = ceil(minutes x 1.3).
    Fare from the city fare table (base + per-km), as a CostRange with basis "category_estimate".
    """

    def __init__(self, cities_path: str | None = None) -> None:
        raise NotImplementedError

    def leg(self, a: Candidate, b: Candidate, ctx: WorldContext) -> Leg:
        raise NotImplementedError
