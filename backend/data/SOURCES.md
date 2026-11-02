# Data sources

Every data file under `backend/data/`, where it came from, its licence and how it may be used.

## `snapshot/bangalore.json`: curated OpenStreetMap snapshot (ODbL)

- **Source:** OpenStreetMap, via the public Overpass API (`https://overpass-api.de/api/interpreter`).
- **Retrieved:** 2 November 2026. The OSM data timestamp reported by Overpass was `2026-11-02T12:48:50Z`.
- **Request:** one bounded query (below), sent with an identifying `SaturdayPlanner/0.1` User-Agent. It returned
  HTTP 200 with 403 elements, and no retries were needed.
- **Curation:** 41 of the 403 elements were kept by hand. The selection favours places near the centre, places with
  OSM diet tags (`diet:vegetarian`, `diet:vegan`), places with `opening_hours`, and well-known parks,
  museums, galleries, bookshops and one concert hall. Chain fast-food outlets and unnamed or ambiguous entries
  were left out.
- **Licence:** © OpenStreetMap contributors, available under the
  [Open Database Licence (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). The snapshot is a
  derivative database and is distributed under the ODbL. Attribute it as "© OpenStreetMap contributors" wherever
  it is shown (every snapshot candidate carries that attribution and a link to its OSM object). See the
  [OSMF attribution guidelines](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines).
- **What is OSM fact and what is curation:**
  - **OSM fact:** each record's `osm_tags`, `name`, `lat`/`lng` (the node position or the Overpass `center` of a
    way or relation), and the OSM object ID and type.
  - **Curation (ours, not OSM):** `category`, `popularity` (default `medium`; `high` only for Hard Rock Cafe
    and Lalbagh, as a labelled heuristic), `curation_note`, and the approximate neighbourhood centroids used to
    centre searches.
- **How it is mapped at runtime** (`app/providers/snapshot.py`):
  - **Diet:** `diet:vegetarian` = `yes` or `only` maps to CONFIRMED, `no` maps to NO, and a missing tag stays
    UNKNOWN. `only` also adds the tag "vegetarian only". `diet:vegan` and `diet:jain` follow the same rules.
    `diet:vegan` = `yes` or `only` also confirms vegetarian, because vegan options are vegetarian.
  - **Wheelchair:** `yes` maps to CONFIRMED and `no` maps to NO. `limited` and a missing tag both map to UNKNOWN,
    since limited access is never treated as a yes.
  - **Opening hours:** `opening_hours` is parsed for the target weekday. Unparseable or missing values give
    "hours unknown"; hours are never invented.
  - **Costs:** OSM has almost no prices, so costs are always the category default and labelled
    `category_estimate`. The only exception is `fee=no` on a non-food place, which is marked `free`.
- **Last checked:** 2 November 2026. This date is shown on every snapshot stop as `last_checked`.

Overpass query:

```overpassql
[out:json][timeout:90];
(
  nwr["amenity"~"^(cafe|restaurant|ice_cream|fast_food)$"]["name"]["diet:vegetarian"](around:6000,12.9716,77.5946);
  nwr["amenity"~"^(cafe|restaurant|ice_cream|fast_food)$"]["name"]["diet:vegan"](around:6000,12.9716,77.5946);
  nwr["leisure"~"^(park|garden)$"]["name"]["wikidata"](around:6000,12.9716,77.5946);
  nwr["tourism"~"^(museum|gallery)$"]["name"](around:6000,12.9716,77.5946);
  nwr["shop"="books"]["name"](around:6000,12.9716,77.5946);
  nwr["amenity"~"^(theatre|arts_centre|music_venue|concert_hall)$"]["name"](around:6000,12.9716,77.5946);
  nwr["natural"="water"]["water"="lake"]["name"]["wikidata"](around:6000,12.9716,77.5946);
);
out center tags;
```

To refresh it, re-run the query politely (once, with an identifying User-Agent, and back off on HTTP 429). Then
re-select the same OSM IDs, check that each still exists with the same tags, and update `last_checked`.

## `fixtures/`: frozen world for tests and model evals (synthetic)

- **`world.json`:**
  - **Contents:** four locations (Bengaluru, Mumbai, London, Jaipur), neighbourhood centroids, 51 Bengaluru and
    13 Mumbai candidates.
  - **Names:** names marked `_fictional` are invented. Other names are real public places with approximate
    coordinates.
  - **Attributes:** **all attributes are synthetic test data**: hours, prices, diet, accessibility, ratings,
    rating counts, popularity, busy windows and tags. They are labelled "Synthetic test attributes" on every
    candidate. They are not claims about the real venues and must not be shown to users as facts.
  - **Provenance:** nothing was copied from Google Maps.
- **`events.json`:** invented schema.org Event JSON-LD on the reserved `example.com` domain. It includes deliberate
  bad cases: a wrong date, a missing source URL, and an injected instruction in the event name.
- **`weather.json`:** synthetic daily weather for Saturday 7 November 2026. Sunrise and sunset approximate the real
  times for each city on that date.

## `price_bands.yaml`: per-person price bands (estimates)

- **What it covers:** hand-set per-person bands for each Google `priceLevel` and currency, plus category default
  levels for currencies other than INR.
- **Status:** these are estimates, labelled `price_level_estimate` or `category_estimate` wherever they are used,
  and were checked on 2 November 2026.

## Live sources (not stored in the repo)

- **Google Places API (New):**
  - **Display:** results are shown with "Google Maps" attribution and a link to `googleMapsUri`.
  - **Storage:** only place IDs may be stored; display fields are re-fetched.
  - **Evals:** never used for model evals. See IMPLEMENTATION_PLAN.md §6.
- **Open-Meteo:** geocoding (GeoNames data) and daily forecasts. The free tier is for non-commercial use only.
  See https://open-meteo.com/en/terms.
- **Tavily and event pages:**
  - **Search:** Tavily search (`Authorization: Bearer` header), restricted to an allow-list that starts with
    `district.in`.
  - **Page fetching:** at most three result pages are fetched, with the `SaturdayPlanner/0.1` User-Agent,
    robots.txt respected, a 5 s timeout and a 1 MB cap.
  - **Parsing:** only schema.org Event JSON-LD is parsed, in code. Descriptions are never kept. Each event links
    back to its page.
