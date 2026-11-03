# Independent audit: backend core (domain, providers, pipeline, baseline, data)

Date: 2026-11-03. Auditor: independent agent (did not write this code). Scope: `backend/app/domain/**`,
`backend/app/providers/**`, `backend/app/pipeline.py`, `backend/app/baseline.py`, `backend/data/**`. Out of scope and
not re-reported: the known "underfilled plan" defect (plans use too little of the window), which other agents are
fixing in `validator.py`, `fill.py` and `app/graph/**`. No existing file was modified. Nothing touched the network.

Reproductions live in `backend/tests/audit/test_audit_core_{parser,planner,infeasible,providers}.py`. Each confirmed
bug is a test marked `@pytest.mark.xfail(strict=True, reason="AUD-<n>: ...")`, so a fix shows up as an XPASS failure
and the marker must then be removed. `--runxfail` was used to check that every one fails on its intended assertion,
not on a setup error.

## 1. Commands run (from `backend/`)

| Command | Result |
|---|---|
| `uv run pytest` (start of audit) | all green (no failures) |
| `uv run ruff check .` | All checks passed |
| `uv run mypy app evals` | Success: no issues found (76 files at start, 77 once `fill.py` appeared) |
| `uv run pytest --ignore-glob='tests/audit/test_audit_core_*'` (end) | 1721 passed, 11 deselected, 24 xfailed (those 24 belong to another auditor's `test_audit_service_*`) |
| `uv run pytest tests/audit -k audit_core` | 1 passed (a control test), 56 xfailed (strict), 0 failed |
| `uv run ruff check tests/audit/test_audit_core_*.py` / `uv run mypy tests/audit/test_audit_core_*.py` | clean / clean |

The suite was re-run at the end because `fallback.py`, `fill.py` and `validator.py` changed during the audit (other
agents' in-flight work). Every finding below was re-confirmed on the final state of those files.

## 2. Method

1. **Structured grid (about 3,000 requests, 6 seeds).** Script-generated `PlanRequest`s through
   `app.baseline.run_baseline` with `build_deps(fixture_settings(fixture_weather=...))`: cities Bangalore, Bengaluru,
   blr, Mumbai, Bombay, London, Jaipur; budgets 0..10000 including odd values (1, 99, 333, 777, 1499, 3333); windows
   30..720 min; starts 06:00..23:30; ten moods (low/medium/high energy); 0-3 interests; 0-3 of 14 constraints in any
   combination; weather clear, rain_85pct and unavailable. Checks were made outside the validator:
   * every complete plan goes through `tests/verification/independent_checks.plan_failures`, which recomputes cost,
     legs, hours, evidence, pacing, prose, daylight and Plan B from the raw data files, plus extra checks: plan
     inside the requested `[start, start+window]`, monotonic clock times, alcohol, setting, must-avoid,
     must-include, currency, no failing check item, sit-down meals inside meal windows, no walk before dawn;
   * for every `no_feasible_plan`, each offered relaxation is applied the way the client does it (a shallow patch of
     the request) and the request is re-run. A relaxation that doesn't produce a plan is a broken promise;
   * an independent brute-force search for a one-stop plan in the fixture world (open at the start, within budget,
     every constraint met with evidence, seated when energy is low, daylight, meal timing). If one exists and the
     system answers `no_feasible_plan`, the request is triaged by hand.
2. **Free-text parse grid.** 43 time phrasings × 6 money phrasings and 5 × 25 money phrasings (Indian English:
   "4.30pm", "1.5k", "₹2,000", "Rs.500", "1200/-", "a couple of hours", "this evening", "between 4 and 8 pm", ...),
   each with a known intended start, window and budget.
3. **Free-text fuzz (1,000 runs).** Random Indian-English sentences (typos, neighbourhoods, ranges, injections, diet
   negations) through the baseline, with a second round `asked_before=True`. Checked for crashes, numbers in prose,
   over-budget and over-window plans.
4. **Targeted probes.** Sun times on DST dates and in both hemispheres, `next_saturday` across time zones, Google
   `regularOpeningHours`/`currentOpeningHours` (overnight, 24/7, missing days, special days), `priceRange`
   (other currency, open-ended, nanos), FX conversion, constraint negations (65 phrasings), clarification after
   unknown cities, partial Google failures (respx), countries missing from `countries.yaml`, same-day starts that
   have passed, `fill_window` interactions with meals, unknown hours, events and Plan B.

Headline: across all of this, **no crash, no plan over budget, no stop outside known hours, no diet, access,
crowd, alcohol, setting or pacing violation against the enforced constraints, no number in model or template
prose**. The core arithmetic is sound. The bugs are in what gets enforced (parsing), what gets searched, and
what the infeasibility report promises.

Time use (known defect, not re-reported): 29-38% of complete grid plans used under 60% of the window.

## 3. Bugs, by severity

P0 = wrong or unsafe result shown to the user; P1 = wrong behaviour in a realistic case; P2 = edge case or cosmetic.

### P0

**AUD-1: "I don't eat meat" is read as "I eat meat": no vegetarian constraint, a non-veg restaurant is planned.**
* Repro: `test_audit_core_parser.py::test_aud1_*`. `constraints=["I don't eat meat"]`, Bangalore, 19:00, ₹3000,
  food+music → plan includes The Malabar Table (`vegetarian: no`), soft preference "No diet restriction", diet check
  "No diet requirement". The same goes for "I do not eat meat", "I dont eat meat" and "we never eat meat", in
  structured constraints and in free text.
* Root cause: `app/domain/constraints.py:94` (`diet_negated`). The pattern
  `\b(i eat|eat|fine with|...)\s+(meat|non-veg|chicken|fish)\b` matches the "eat meat" inside "don't eat meat", so
  vegetarian is treated as negated and `negates_all_diets` emits "No diet restriction". The vegetarian rule
  (`constraints.py:31`) only knows "no meat", not "don't eat meat". This also defeats the LLM path:
  `parser_rules._clause_supports` (`parser_rules.py:1300`) drops a model's correct `diet:vegetarian` because
  `diet_negated` says it is negated (`test_aud1_llm_vegetarian_constraint_survives_merge`).
* Fix: add a negative lookbehind for `don't|do not|dont|never|can't|cannot|won't|not` before `eat`; map
  `(don't|do not|never|can't|won't) eat (meat|non-veg|chicken|fish)` to `diet:vegetarian`; add the phrase to
  `_SUPPORT_POSITIVE["diet:vegetarian"]`.

**AUD-2: "2pm to 6", "4.30pm to 8", "6 pm till 10", "11am to 2" become 12-hour windows; plans run past the stated
end.**
* Repro: `test_aud2_*`. "I'm free from 4.30pm to 8 in Bangalore with 1.5k" → window 720 min, plan 4:30 PM → 10:21
  PM. The time check still passes ("Uses 351 of 720 min"). "free 2pm to 6" → 720 min too: today's plan happens to
  end at 5:50 PM, but any fill or stretch can push it to 2 AM.
* Root cause: `app/domain/timeutil.py:119-130` (`parse_time_range`). A bare end is parsed as a 24-hour clock (6 → 06:00).
  Only a bare *start* borrows the other side's am/pm. Then `end <= start` adds 24 h, giving 16 h, which
  `_clamp_minutes` cuts to 720.
* Fix: when only the start has am/pm and the bare end is ≤ 12, try `end + 12h` (pm) or the same meridiem first and
  take the first value that lands within 12 h after the start ("11am to 2" → 14:00, "2pm to 6" → 18:00).

**AUD-3: "between 4 and 8 pm" plans 8 PM to midnight; "between 4pm and 8pm" ignores both times.**
* Repro: `test_aud3_*`. "Bangalore, between 4 and 8 pm, budget 1000" → start 8:00 PM, window 240, plan 8:00 PM → 10:28 PM,
  all of it after the user's window. "between 4pm and 8pm" → two bare tokens, no start, default 3:00 PM.
* Root cause: `timeutil.py:110-113`. The range pattern has no `and` separator and no `between` cue.
  `parser_rules._extract_time` (`parser_rules.py:551`) then makes the lone "8 pm" the start.
* Fix: accept `between X and Y` (and `X and Y` after `between`) in `parse_time_range`.

### P1

**AUD-4: the "best" beam search misses valid plans that need shorter-than-default stops. Wrong `no_feasible_plan`
results, and "Raise budget"/"Allow N min" relaxations that don't work.**
* Repro: `test_audit_core_planner.py::test_aud4_*` (3 cases). Example: Bangalore, ₹615, 15:00-17:00, vegan + include
  live music. `search_plans(objective="best")` returns None, but `objective="cheapest"` returns a valid plan
  (Freedom Park 30 min → Lavelle Road Music Room 60 min, `report.ok`). The same happens in Mumbai with three
  must-includes in 182 min, and with sit-down meal + museum in 110 min. End to end, for ₹333 the report offers
  "Raise budget to ₹615" and "Allow 2 h 4 min", and neither works (`test_aud4_offered_relaxations_produce_a_plan`).
  About 25 grid relaxations failed this way.
* Root cause: `app/domain/fallback.py:578-588` (`_beam`). For each (node, stop) it `break`s after the first
  duration option that fits ([default, lo] for "best"), and children are keyed by `(frozenset(idx), j)`. So the
  lo-duration variant of an earlier stop is never explored when the default fits locally but blocks a later
  required stop. `infeasible.py` computes `cheapest_budget`/`min_minutes` with lo durations (cheapest/shortest),
  and then promises them to the "best" search. The graph's fallback node (`app/graph/nodes/fallback.py:78,94`) uses
  the same `plan_deterministic`.
* Fix: keep both duration variants (no `break`; key children by `(frozenset(idx), j, dur)`), or at least make
  `plan_deterministic` fall back to `search_plans(objective="shortest")` when "best" finds nothing.

**AUD-5: budget relaxations carry no currency. With a structured foreign currency, "Raise budget to ₹275" plans
against ₹26,125.**
* Repro: `test_audit_core_infeasible.py::test_aud5_*`. `{"city": "Bangalore", "budget": 1, "currency": "USD", ...}`
  → "$1 ≈ ₹95", infeasible, "Raise budget to ₹275" with patch `{"budget": 275}`. On re-run, 275 is read as USD:
  "$275 ≈ ₹26,125". The reverse direction (structured INR in London) shrinks the budget instead.
* Root cause: `app/domain/infeasible.py:172` and `:250`. Only the empty-pool patch (`:143`) adds
  `"currency": plan_currency(ctx, prefs)`.
* Fix: add the plan currency to every budget patch.

**AUD-6: no place data for the city, but the report blames prices and offers a useless "Raise budget".**
* Repro: `test_aud6_*`. With the keyless default (`PLACES_PROVIDER=auto` without a Google key resolves to
  `osm_snapshot`, which covers Bangalore only), "Mumbai, ₹1000" gives "Nothing affordable turned up in Mumbai for a
  budget of ₹1000; places there may simply cost more" and "Raise budget to ₹1500". Tapping it gives "No places or
  events were found for Mumbai". Same for London £30 → "Raise budget to £40". About 100 grid relaxations failed
  this way.
* Root cause: `infeasible.py:134-148` assumes an empty pool means price filtering whenever the budget is below
  the default. `baseline.py:81` never checks `deps.places.covers(location)`.
* Fix: check provider coverage first and say "no place data for <city> yet" without relaxations. Keep the
  affordability message for when the queries actually carried a `max_price_level`.

**AUD-7: relaxations are tested on the current candidate pool, but applying one re-runs a different search.**
* Repro: `test_aud7_*`. Mumbai, outdoors only, 22:00, rain → "Start at 3:00 PM" is offered (true for the 22:00 pool).
  The 15:00 re-run builds a pool with no outdoor place (see AUD-8) → no plan. Bangalore, outdoors only + wheelchair,
  45 min → "Drop 'outdoors only'". The re-run pool has no wheelchair-confirmed place ("rules out 9 of 9").
* Root cause: `explain_infeasible` runs every relaxation test (`infeasible.py:157-260`) on `unique`, the pool
  gathered by `default_queries` for the *original* preferences. `default_queries` depends on start time, meal
  windows, weather and constraints.
* Fix: re-run `default_queries` and search with each relaxed setting before offering it (cap the number of
  relaxations), or merge the old pool into the re-run's ledger.

**AUD-8: "outdoors only" on a rainy day that spans a meal: no outdoor category is searched at all.**
* Repro: `test_aud8_*`. Bangalore, outdoors only, food+art, 11:00 for 6 h, rain → queries `restaurant, cafe,
  restaurant`, 0 of 16 candidates outdoor → "Keeping to 'outdoors only' rules out 16 of 16 places. Dropping
  'outdoors only' would make a plan possible". Parks are open, and rain only raises a warning (other grid requests
  do get outdoors-only rain plans).
* Root cause: `app/domain/parser_rules.py:1842-1851`. When rainy, the park filler becomes "art gallery", which
  outdoors-only then skips. The later park filler is only reached below `MIN_QUERIES`, which meal queries already
  fill. Garden, lake, heritage walk and viewpoint are never queried.
* Fix: under `setting:outdoors_only`, always add outdoor categories (park, garden, lake, heritage_walk, viewpoint)
  regardless of rain.

**AUD-9: same-day request whose start has passed: the window is shifted, not shortened, so the plan runs past the
user's stated end.**
* Repro: `test_audit_core_providers.py::test_aud9_*`. On Saturday at 18:15 IST, "free 4pm to 8pm today" (or
  `start_time 16:00, available_time 240`) → plan 6:30 PM → 10:28 PM.
* Root cause: `app/pipeline.py:52-58` moves `prefs.start_min` to the next quarter hour, and `:84` recomputes
  `end = start + prefs.available_minutes`.
* Fix: keep the original end. Shrink `available_minutes` to the time left (and say so), and if less than 30 min is
  left, plan next Saturday or say the window has passed.

**AUD-10: one failed live query (a 429 or a timeout) throws away every other live result.**
* Repro: `test_aud10_*` (respx). 3 of 4 Google text searches succeed and 1 returns 429 → 0 candidates. For
  Mumbai (no snapshot) the result is "Live place data is temporarily unavailable".
* Root cause: `pipeline.py:112`. `asyncio.gather` runs without `return_exceptions`, so it is all-or-nothing, and
  the snapshot fallback only helps Bangalore.
* Fix: gather with `return_exceptions=True`, keep the successful batches, and fall back (or degrade) only when
  every query failed.

**AUD-11: cities in countries missing from `countries.yaml` (Belgium, Greece, Finland, Poland, ...) are priced in
USD, and their listed local prices are thrown away.**
* Repro: `test_aud11_*`. A Brussels cafe with Google `priceRange` €8-15 becomes `CostRange(20, 45, "USD",
  price_level_estimate)`. With a € budget, every candidate fails the currency check, so the city is unplannable.
  With a bare budget, the currency question offers only INR/USD.
* Root cause: `app/providers/normalize.py:156-158` (`location_currency` falls back to `"USD"`), plus the coverage of
  `data/countries.yaml`. The geocoder resolves any city, so this isn't limited to the gazetteer.
* Fix: add every euro-area country (and common destinations) to `countries.yaml`. For an unknown currency, take
  it from the place's own `priceRange.currencyCode` or mark costs unknown. Never relabel as USD.

**AUD-12: an open-ended Google `priceRange` ("£40+", "₹2,000+") is costed with max == min, which understates the
worst case.**
* Repro: `test_aud12_*`. `startPrice` £40 only (+ MODERATE band 15-35) → `CostRange(40, 40)`; `startPrice` ₹2000
  only → `(2000, 2000)`.
* Root cause: `app/providers/google_places.py:250-255`, where `hi = max(lo, fallback.max)`. For the top bucket, which
  is exactly where budgets bind, this collapses to `lo`.
* Fix: give open-ended ranges headroom (e.g. `max(lo * 1.5, band.max)`, or the next band up) and say in the
  label that the price is open-ended.

### P2

**AUD-13: `fill_window` breaks the planner's realism rules (meal timing, unknown hours only in the daytime).**
(`fill.py` is in-flight work; this is a different defect from "underfilled".)
* Repro: `test_aud13_*`. Bangalore 14:00, 12 h, coffee → Airlines Hotel 2:00-3:50 PM (lunch window ends 2:30).
  Mumbai 10:00 → Kailash Parbat 10:00-11:50 AM. Snapshot, 20:00 → Sapna Book House (unknown hours) 9:25-10:40 PM,
  past the 22:00 cap. The control test with `fill_window` disabled keeps every meal in its window.
* Root cause: `app/domain/fill.py:88-90` accepts any stretch that `validate_plan` passes. Meal timing
  (`fallback.py:420`) and the unknown-hours day window (`fallback.py:524`) are planner-only rules that the validator
  doesn't know.
* Fix: have `fill_window` cap a stop's stretch with `meal_slot`/`_plausible_unknown_hours`, or move those two
  rules into the validator as warnings that `fill_window` must not introduce.

**AUD-14: outdoor walks are allowed before sunrise.** `test_aud14_*`: start 05:30 → Bugle Rock Park 5:30-6:37 AM,
with sunrise 6:13. `validator.py:453` and `fallback.py:412` compare only with sunset. Fix: also require
`start >= sunrise` (sunrise is already in `Weather`).

**AUD-15: structured `available_time` text isn't clamped to 30..720.** `test_aud15_*`: "15 hours" → 900 min,
"5 min" → 5, "0.2 hours" → 12. The `PlanRequest._bounded_time` docstring promises "Text is parsed later and
clamped to the same range", but `parser_rules.py:1093` uses `parse_duration` raw. Fix: `_clamp_minutes(...)`, plus
an assumption note when clamping.

**AUD-16: "an hour and a half" → 60 min; "2 and a half hours" and "1 and a half hours" aren't read (240-min
default).** `test_aud16_*`. `_DURATION_RE` (`parser_rules.py:470`) stops at "an hour", and `parse_duration` has no
"N and a half" form. The shorter error is harmless; the default can over-fill.

**AUD-17: an amount the parser can't attribute is replaced by a higher default budget.** `test_aud17_*`: "1200/-"
(the common Indian rupee suffix), a trailing bare amount ("Bangalore, 3:30 to 6:30, 1000") and "2 hrs and 30 mins,
500" all get the ₹1500 default, above what the user wrote. The swap is disclosed ("Couldn't tell whether “1000” is
your budget"). "budget 1000-1500" silently becomes 1500, with no note. Causes: `_MONEY_SUFFIX`
(`parser_rules.py:337`) lacks `/-`; `_NOT_MONEY_UNIT` rejects ranges; `apply_defaults` (`:1673`) applies the default
even when an unread amount exists. Fix: recognise `/-` and ranges (take the lower bound), and use
`min(default, unread amount)` when an unread amount exists. ("2 grand" is also unread, but there the default is
lower, so it's conservative.)

**AUD-18: permissive phrasings map to the opposite constraint.** `test_aud18_*`: "no problem with crowds" →
`crowd:avoid`, "no issues with drinks" → `alcohol:avoid`, "veg and non-veg both fine" → `diet:vegetarian`
(`constraints.py:31-37`: `no ... .{0,20} crowds|drinks` and a bare `veg`). "no wheelchair needed" maps correctly to
nothing but is then flagged as unmapped hard text and asked about. This over-constrains rather than being unsafe.

**AUD-19: an unknown city after the clarification round is reported as an outage.** `test_aud19_*`:
"Atlantisville" with `asked_before=True` → "I couldn't look up that city right now. Please try again shortly."
`baseline.py:70` treats `matches == []` (not found) like `None` (geocoder error).

**AUD-20: `priceRange` nanos are dropped, so a listed maximum of £14.99 becomes £14.** `test_aud20_*`.
`google_places._units` (`:227`) reads `units` only. Fix: ceil the upper bound and floor the lower one.

**AUD-21 (documented limitation, poor outcome): "from 5 to 9", "3 to 6", "4 hrs starting at 4", "6 to 9 in the
evening" are ignored and a 15:00 start is assumed, so the plan starts before the user is free.** `test_aud21_*`.
LLD §3.1 says bare ranges are skipped as ambiguous, but a `from`/`free`/`starting` cue with small hours is not
ambiguous. The existing "before 07:00 → afternoon" heuristic could apply.

**AUD-22: baseline search ignores time of day and budget, so it answers "no plan" when an easy one exists.**
`test_aud22_*`: Bangalore 23:00, ₹500 → "Even with an unlimited budget and a longer window, nothing found fits this
start time", yet Midnight Scoops (open until 01:00, ₹120-280) alone is a valid plan. ₹0, indoors only, 17:00, rain →
"cheapest valid plan costs ₹22", yet Rangoli Metro Art Center is free and open until 20:00. About 50 such misses per
600 grid requests (late starts, ₹0-150 budgets, outdoors-only, breakfast queries limited to cafes). Cause:
`default_queries` (`parser_rules.py:1771-1860`) has no late-night, free or setting-aware queries. The infeasibility
sentence (`infeasible.py:287`) presents "nothing in this pool" as "nothing in the city".

## 4. Near-misses that turned out fine

* **Hard-constraint enforcement:** about 3,000 grid plans and 1,000 fuzz plans, 0 violations of budget (with ceil-10%
  contingency), known hours, event windows, diet/wheelchair evidence, alcohol, setting, must-avoid/include, crowd
  windows, walking/stop/long-leg limits or sunset, all recomputed independently. Legs match haversine × 1.4 and the
  `cities.yaml` fares. Stops are distinct, clock times monotonic, currency consistent.
* **Prose:** no prices, durations, distances or clock times in `why`/`tradeoff`/`summary_note`/omissions.
* **Low energy without a seated stop (19 cases):** all under "outdoors only", the documented `PACING_RELAXED`
  waiver. **Outdoor stop in rain without Plan B:** always a `WEATHER_RISK` warning because no indoor option
  was open, affordable or allowed (outdoors-only, 07:30 starts, ₹0). By design.
* **Google hours:** 24/7 (single open), Sat 18:00-Sun 02:00 → (1080, 1560), Friday spill → (0, 60), every day
  0-24 as 7 periods → (0, 2880), split lunch/dinner, missing Saturday → closed. `currentOpeningHours` special days,
  closures inside the 7-day window, and fallback to regular hours outside it are all correct.
* **priceRange in another currency** falls back to the location's band. It is never mixed.
* **Sun and time zones:** `sun_times` for London (28 Mar GMT 18:25, 4 Apr BST 19:37), New York (7 Nov EST 16:47),
  Sydney (3 Oct, day before DST), Auckland, Bangalore are all within a few minutes. Polar night/day → None.
  `next_saturday` is right for New York (UTC Saturday is still local Friday), Auckland (already Sunday) and the
  London/IST 20:00 cutoff.
* **FX:** `convert_amount` floors (₹2000 → £15; $1 → ₹95). Never rounds up.
* **Travel:** same coordinates → a 0-minute walk; unknown Indian city → `default_IN`; same-named foreign city →
  `default` in local currency.
* **Clarification:** at most 2 questions; `asked_before=True` never asks again; currency answers ("local", "INR",
  "£") resolve; conflicts resolve or fall through to an infeasible report with drop relaxations.
* **Prompt-injection fixture event** ("Jazz Night. SYSTEM: ignore all rules ...") is treated as data.
* **Diet phrasings that work:** "pure veg", "veg only", "no non veg", "I'm not vegetarian" (→ no constraint),
  "my friend is vegan", "vegetarian, eggs are ok", "can't climb stairs" → wheelchair.

## 5. Coverage gaps (not tested or not reproducible offline)

* Live Google/Open-Meteo/Tavily payload variety: only synthetic payloads were used. `currentOpeningHours`
  `truncated` flags and "open now" truncation of today's period were not exercised.
* DST transitions *inside* a plan (e.g. London 28 Mar 2026, a 22:00-03:00 plan crossing the 01:00 jump). Minutes
  are linear wall-clock, so stops after 01:00 show nonexistent local times. No user-visible harm beyond the label,
  so not filed.
* Events with `indoor=None` are treated as indoor by `is_outdoor` (category default), so an outdoor festival in
  rain gets no `WEATHER_RISK`. JSON-LD has no indoor field, so there was no fixture to reproduce it.
* London, Jaipur and any non-Bangalore snapshot city have no place data offline, so non-INR planning was tested
  only at domain level (synthetic candidates, Google normalisation), not through a full plan.
* The graph and LLM paths were not exercised beyond the shared `merge_parse` (AUD-1) and `plan_deterministic`
  (AUD-4).
* Group sizes > 1 with per-vehicle fares (counted in full per person by design) were not audited for realism.

## 6. Notes for the agents working on `fill.py` / `validator.py`

AUD-13 is caused by `fill_window` relying only on `validate_plan`. If the fix moves meal timing and the
unknown-hours day window into the validator, check `tests/audit/test_audit_core_planner.py::test_aud13_*`. They
will XPASS and need their markers removed. AUD-4 lives in `fallback._beam`, next to the code `fill_window` is
wired into (`fallback.py:639-645`).
