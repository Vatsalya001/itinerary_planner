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
  levels for currencies other than INR. Every currency in `countries.yaml` has a band table.
- **Source:** our own judgement of typical per-person spend for a casual visit. No menu or price survey was used.
  INR, GBP, USD and EUR were set first. The other currencies were set later, on the same date, from the USD bands,
  the reference rates in `fx.yaml` and a rough sense of local price levels. They are rougher still.
- **Status:** these are estimates, labelled `price_level_estimate` or `category_estimate` wherever they are used,
  and were set on 2 November 2026 (`checked: 2026-11-02`). They are not quotes and were not checked against real
  venues.
- **Licence:** project data.

## `categories.yaml`: canonical place categories (project heuristics)

- **What it covers:** the 23 category keys every provider maps onto. Each has a group, meal/indoor/seated/walking
  flags, effort, a visit duration range, an alcohol flag, a default INR cost (`cost_inr`), interest tags and
  typical Saturday busy windows.
- **Source:** hand-written project configuration, added on 2 November 2026. No external dataset is recorded as
  its source.
- **Status:** durations, `cost_inr` and busy windows are heuristics. Costs derived from `cost_inr` are labelled
  `category_estimate`. Busy windows only feed the "likely busy" heuristic and are never shown as live crowd data.
- **Licence:** project data.

## `cities.yaml`: travel speeds, pickup times and fares (estimates)

- **What it covers:** door-to-door speeds and pickup minutes per mode, and auto and cab fare tables for Bangalore,
  Mumbai, Delhi, other Indian cities (`default_IN`) and everywhere else (`default`, keyed by currency).
- **Source and status:** each fare entry carries its own `source` and `as_of`. The Bangalore, Mumbai and Delhi
  auto tariffs match news reports of the notified fares, but were not checked against the official notifications.
  Every cab fare and every `default*` entry is labelled `estimate (unverified)`. That includes the cab fares added
  for the currencies of every gazetteer country, which are rough figures set on 2 November 2026. Speeds and pickup
  minutes are heuristics. Fares are always shown to users as estimates.
- **Licence:** project data. The tariff figures themselves are public facts.

## `countries.yaml`: country to currency and display symbol

- **What it covers:** an ISO 3166-1 alpha-2 country code mapped to its ISO 4217 currency and a display symbol, for
  every country used in `gazetteer.yaml` (34 countries) and no others. In a country that is not listed, a bare
  budget number triggers a currency question.
- **Source:** public reference facts (ISO 4217 currency of each country). Checked by hand on 2 November 2026.
  Where the usual symbol is ambiguous or written after the amount (for example `kr`, `Kč`, `Rs`, `CHF`), the ISO
  code is used as a prefix instead.
- **Licence:** public facts, no licence needed.

## `fx.yaml`: reference exchange rates (approximate, not live)

- **What it covers:** `units_per_usd` for every currency in `countries.yaml`. These rates only convert a budget
  stated in one currency into the planning city's currency. The plan says so in an assumption
  ("≈ ... at an approximate reference rate (2026-11-02, not live)").
- **Source:** mid-market quotes captured on 2 November 2026 (`as_of`). The file header lists, per currency, which
  public quote pages were used: Trading Economics, OFX, X-Rates, XE, Bloomberg, the RBA, the ECB's 30 October 2026
  reference rates, exchangerates.org.uk, Wise and Pluang. Pegged currencies (AED, QAR, HKD, NPR, DKK) were checked
  against their pegs or bands.
- **Status:** approximate and rounded. They are never presented as live, and a converted budget is rounded down.
  Refresh them by hand before relying on them.
- **Licence:** individual rates are facts. No provider's table is redistributed.

## `gazetteer.yaml`: cities, aliases and neighbourhoods

- **What it covers:** 93 cities in 34 countries, each with a country code, IANA timezone, approximate centre and
  aliases, plus neighbourhood centroids for Bangalore, Mumbai and Delhi.
- **How it is used:** the rules parser uses it to spot place names in free text, the baseline uses it to bias
  searches towards a neighbourhood, and the live geocoder consults it first (it is the fallback Location when
  Open-Meteo has no match or fails).
- **Source:** hand-compiled for this project, added on 2 November 2026. The file does not record a source for
  each entry. Coordinates are approximate centre points (public geographic facts, rounded to 4 decimals), good
  enough for a search bias and a 40 km sanity check but not for routing. Names that are common English words or
  brands are deliberately left out.
- **Licence:** project data. Place names and approximate coordinates are public facts.

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
  - **Page fetching:** at most three result pages are fetched concurrently, with the `SaturdayPlanner/0.1`
    User-Agent, a 5 s timeout (4 s to connect), a 1 MB cap and a 15 s budget for the whole search. robots.txt is
    respected. It is cached per site for 24 hours, and a failed fetch counts as "disallow" for 1 hour.
  - **Parsing:** only schema.org Event JSON-LD is parsed, in code. Descriptions are never kept. Each event links
    back to its page.
