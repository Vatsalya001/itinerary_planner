# Phase 1 verification report

Independent verification of the deterministic core (domain, rules parser, providers, data, baseline pipeline,
CLI), run on 2 November 2026 against the working tree. `backend/app/graph`, `backend/app/llm` and their tests were
out of scope (being edited concurrently for Phase 2).

> **Update (re-verification after the D-01..D-27 fixes): see §8.** Sign-off is still withheld:
> * 25 of the 27 defects are fixed by behaviour, and 2 are partially fixed (D-07, D-22).
> * The fixes introduced one P0 regression: "no non-veg" and similar phrases now lose the diet (R-4).
> * There are 3 P1 regressions: am/pm flips (R-1), 12-hour windows for ranges across noon (R-2), and evening or
>   late-morning plans lost to the meal-window rule (R-3).
>
> Sections 1–7 below are the original report.

**Verdict (original run).** The baseline's arithmetic is right. Independent recomputation of cost, legs, schedule, hours,
evidence, pacing, prose, daylight and Plan B matches the returned plan on every feasible fixture case, and all 889
existing tests pass. Phase 1 is still not ready to sign off, for four reasons:

* **3 constraint-safety holes:**
  * a cross-currency budget bypass (D-01);
  * a dropped Jain constraint (D-02);
  * food events that skip diet evidence (D-03).
* **1 parser defect that silently produces wrong plans** for common Indian input. "4.30pm" moves the window to
  8 PM, and "600 budget" becomes ₹1,500 (D-07).
* **Mypy is not clean** on Phase 1 files (D-24).
* **Several plan-quality problems** show up in the brief's own example. The main one is a full thali at 3 PM
  (D-09).

All file and line references below are relative to `backend/`. Every defect has a reproduction, and most are
encoded as `xfail(strict=True)` tests under `backend/tests/verification/`. Those tests assert the correct
behaviour, so a fix makes them XPASS, and they then need un-marking.

---

## 1. Defects (prioritised)

### P0: must fix

**D-01: Budgets are checked across currencies without conversion.** The check silently passes over-budget plans.

* **Where:**
  * `app/domain/costs.py:34-57`: `compute_totals` sums `cost.max` and `fare.max` whatever their `currency`.
  * `app/domain/schedule.py:26-33,138-141`: the totals currency is `prefs.currency`.
  * `app/domain/validator.py:308`: compares the sum with the budget. No check anywhere that the stop and leg
    currencies equal the budget currency.
* **How the flow reaches it:**
  * `app/domain/parser_rules.py:1078` offers `[GBP, INR]` for a London budget.
  * `apply_defaults` (`parser_rules.py:1284`) keeps an explicit INR.
  * Google prices London places in GBP (`normalize.location_currency`).
  * Travel fares fall back to USD for currency-less locations (`travel.py:135`, `normalize.py:176`).
* **Reproduction:** `test_phase1_adversarial.py::test_london_with_an_inr_budget_never_shows_a_cross_currency_plan`.
  * Setup: respx-mocked Google, fixture geocoder, brief request with `city=London`, answer `{"currency": "INR"}`.
  * Result: a `complete` plan whose stops are priced in GBP. It reports "worst case 116" against a ₹2000 budget, but
    £116 is roughly ₹12,000.
  * Validator-level repros: `test_currency_mismatch_between_candidates_and_budget_is_flagged` and
    `test_leg_fare_currency_mismatch_is_flagged`.
* **Fix:**
  * Add a `CURRENCY_MISMATCH` error in `validate_plan` whenever any stop, leg or Plan B currency differs from the
    budget currency.
  * Make `plan_currency` the location currency.
  * When the user's currency differs from the local one, convert with a dated, labelled rate table, or keep asking.
  * Never price legs in a fallback currency.

**D-02: "Vegan" plus "Jain" keeps only one diet, and Jain is silently dropped.** No `CONSTRAINT_DROPPED` warning is
raised either.

* **Where:**
  * `app/domain/constraints.py:62-66` (`map_constraint_text`) and `:106-109` (`merge_constraints`) keep the first
    of vegan/jain.
  * `original_constraints` (`parser_rules.py:766-771`) merges the same way, so the safety net loses it too.
* **Reproduction:**
  * `parse_rules(PlanRequest(free_text="Bangalore, I'm vegan and jain, 2000 rupees"))` gives `hard=['diet:vegan']`.
  * `test_vegan_and_jain_are_both_enforced`: a vegan-confirmed restaurant with Jain evidence UNKNOWN validates `ok`.
* **Fix:** vegan and Jain are not nested (Jain allows dairy and excludes root vegetables), so keep both. Only collapse
  `vegetarian` into a stricter diet.

**D-03: Events are never food stops, so a FoodEvent passes any diet constraint with no evidence.**

* **Where:**
  * `app/domain/evidence.py:48-52`: `is_food_stop` uses the `event` category (group `event`, `meal: false`).
  * `app/providers/events_web.py:68`: tags FoodEvent with `food`. Name regex: `food|brunch|dinner|tasting|market`.
  * The fallback summary then claims "Every food stop has confirmed vegetarian options"
    (`app/domain/fallback.py:486-488`).
* **Reproduction:** `test_food_event_needs_diet_evidence`. A JSON-LD FoodEvent "Kebab and Grill Dinner Night"
  validates `ok` under `diet:vegetarian`.
* **Fix:** treat events tagged `food` (FoodEvent, or food words in the name) as food stops with diet UNKNOWN. That
  way, unknown is never yes.

**D-07: The rules parser misreads very common time and budget phrasings.** It silently produces a plan for the wrong
window or budget.

* **Where:**
  * **Dotted times:**
    * `app/domain/parser_rules.py:427-428`: the `_CLOCK_TOKEN` lookbehind rejects the digit after `.`.
    * `app/domain/timeutil.py:84`: `parse_clock` strips `.`, so "4.30pm" becomes "430pm" and is rejected.
    * `timeutil.py:104` (`parse_time_range`) only accepts `:`.
    * The lone "8pm" is then taken as the start (`parser_rules.py:496`).
  * **Number before the cue word:**
    * `parser_rules.py:314-318`: `_MONEY_CONTEXT` needs a cue word before the number.
    * So "600 budget", "2500 max" and "with 2500" are never read.
    * `apply_defaults` (`parser_rules.py:1290`) then substitutes ₹1,500.
* **Reproduction:**
  * **Wrong window:** `cli plan --fixture --free-text "Bangalore, free from 4.30pm to 8pm, 1500 rupees, love coffee"`
    plans **8:00 PM–10:28 PM**. The user's whole window is outside it, and no assumption is shown.
  * **Over budget:** `--free-text "Bangalore, 600 budget, 4 hours, coffee and music, feeling tired"` gives a worst case
    of **₹737 against the user's ₹600**. The only disclosure is "Budget assumed to be ₹1,500".
  * **Tests:** `test_dotted_clock_times` and `test_amount_before_budget_word[*]`.
* **Fix:**
  * Accept `h.mm` with am/pm, or when it sits in a range.
  * Add `<num> (budget|max|tops|per head)` and `with <num>` patterns.
  * If a bare number is present but unread, ask instead of defaulting.

### P1: should fix

**D-04: When the weather provider fails, sunset is unknown, so outdoor walks can be scheduled after dark.**

* **Where:**
  * `app/domain/validator.py:284,300`: the DAYLIGHT check is skipped when `sunset_min is None`.
  * `app/domain/fallback.py:286`: same skip in the planner.
  * `pipeline.py:43-50` carries no fallback.
* **Reproduction:** `test_no_outdoor_walk_after_dark_when_weather_is_unavailable`.
  * Request: Mumbai, 19:30, 3 h, walks/views, fixture weather `unavailable`.
  * Result: Marine Drive Promenade (an outdoor walking viewpoint) is scheduled 7:30–8:15 PM. Sunset is about 18:05.
  * With `clear` weather the same request excludes it.
* **Fix:** compute sunset astronomically (a pure function of latitude, longitude and date) when the forecast is
  missing, or use a conservative default.

**D-05: On a Saturday afternoon, the plan starts in the past.**

* **Where:**
  * `app/domain/timeutil.py:10-19`: "today" counts until 20:00.
  * `parser_rules.py:891`: the default start is 15:00.
  * Nothing compares the start with `now`. Also, `PlanRequest.date` accepts any date, including 2020-01-04, which
    plans a past day (`schemas.py:70`).
* **Reproduction:** `test_saturday_evening_request_never_plans_in_the_past`. With `fixed_now` = Saturday 18:00 IST,
  the plan runs 3:00 PM–6:59 PM today.
* **Fix:**
  * When the target is today, start no earlier than `now` plus a margin.
  * Roll to next Saturday when the remaining window is too short.
  * Bound `PlanRequest.date` to the range from today to today plus 14 days.

**D-06: Plan B is validated without time or budget.**

* **Where:**
  * `app/domain/validator.py:194-215`: `plan_b_problems` has time-independent checks only. It never checks opening
    hours during the stop, never checks that Plan B is not another stop in the plan, and never checks the
    worst-case cost if the user switches.
  * `app/domain/fallback.py:410-421`: `_plan_b` sorts closed options last but still returns one when nothing is open.
* **Reproduction:**
  * `test_plan_b_closed_at_the_stop_time_is_flagged`: the museum opens at 17:00 for a park visit at 15:00–15:45.
  * `test_plan_b_duplicating_another_stop_is_flagged`.
  * `test_plan_b_that_would_break_the_budget_is_flagged`: a ₹1,500–2,500 spa as Plan B on a ₹600 plan.
* **Fix:**
  * Check Plan B with `opening_status` over the stop's window.
  * Reject Plan B IDs that are already stops.
  * Include `max(stop, plan_b)` cost in the worst case, or warn.

**D-08: The prose guard (`PROSE_NUMERIC_CLAIM`) misses everyday phrasing.** This matters as soon as model prose
arrives in Phase 2.

* **Where:** `app/domain/validator.py:101-120`.
* **Reproduction:** `test_prose_detector_misses[*]`. None of these is flagged:
  * "spend an hour here"
  * "about an hour"
  * "a few minutes away"
  * "1.5k rupees"
  * "arrive by noon"
  * "open till midnight"
  * "a half-hour stroll"
  * "an hour-long walk"
* **Fix:** add `an?|few` with hour/minute units, `half-hour`, `hour-long`, `noon`/`midnight`, and a `k` multiplier
  with currency words.

**D-09: Meals are scheduled at odd times and still described as well timed.**

* **Where:**
  * `app/domain/fallback.py:250-257`: `_meal_hit` counts a single minute of overlap.
  * The quality and scoring functions never penalise a restaurant stop outside lunch or dinner
    (`fallback.py:328-336`, `scoring.py:171`).
  * `_why` claims "a proper meal at a sensible time" on any overlap (`fallback.py:437-439`).
* **Reproduction:**
  * **Brief's own example:** Shree Jain Bhojanalaya, a thali restaurant, at **3:00–4:20 PM**, in both clear and rain
    (`test_brief_example_does_not_schedule_a_full_meal_mid_afternoon`).
  * **Family case** (`holdout_family_long_day`): the only meal runs 17:56–19:46 (16 of its 110 minutes fall inside
    dinner) and is labelled "a proper meal at a sensible time".
  * **High-energy day:** two full restaurant meals at 12:17 and 15:57.
* **Fix:**
  * Penalise `meal` categories whose start is outside lunch or dinner windows.
  * Prefer cafés, desserts or street food in the snack window.
  * Count a meal hit only when the start, or most of the visit, is inside the window.

**D-10: Idle waits before events are unbounded and counted as "time used".**

* **Where:**
  * `app/domain/fallback.py:272-276`: waiting is always allowed.
  * `fallback.py:332`: `used` includes the wait.
* **Reproduction:** `test_no_long_idle_gap_before_an_event`.
  * Request: 10 h, relaxed, with a partner.
  * Result: lunch ends 2:24 PM, then **173 idle minutes** before the 5:30 PM event.
* **Fix:**
  * Cap waiting (for example at 45 minutes) or fill the gap with a stop.
  * Exclude waiting from the time-use term.

**D-11: A chill outdoor morning is "impossible", and the explanation blames the wrong constraint.**

* **Where:**
  * `app/domain/pacing.py:13`: `min_seated_stops=1` for low energy, but no outdoor category is seated.
  * `app/domain/infeasible.py:125,199`: the reason never mentions pacing.
* **Reproduction:** `cli plan --fixture --start 10:00 --mood chill --interest walks --interest nature --constraint
  "outdoors only"` returns `no_feasible_plan` with this message:

  > "Even with an unlimited budget or a longer window, nothing found fits... Keeping to 'outdoors only' rules out
  > 15 of 21 places."

  The real binding rule is the hidden seated-stop requirement (`test_chill_outdoor_walk_morning_is_feasible_or_names_the_pacing_rule`).
* **Fix:**
  * Waive or relax `min_seated_stops` under `setting:outdoors_only`, or treat benches, lakes and gardens with
    seating as seated.
  * Name pacing in `explain_infeasible`.

**D-15: Event-dependent venues with unknown hours are booked as standalone visits.**

* **Where:**
  * `app/domain/scoring.py:178`: unknown hours cost only -0.5.
  * No rule stops scheduling a concert hall or theatre outside an event.
* **Reproduction:**
  * `high_energy_long_day`: Chowdiah Memorial Hall at **10:00–11:30 AM** (hours unverified)
    (`test_event_dependent_venues_with_unknown_hours_are_not_standalone_stops`).
  * The snapshot-fallback plan (`places_provider_outage`) also uses it, so 2 of 3 stops are "hours unverified".
* **Fix:** exclude, or heavily penalise, `live_music_venue` and `theatre` places with unknown hours unless they are
  backed by an event.

### P2: nice to have

| ID | Defect | Where | Reproduction | Suggested fix |
|---|---|---|---|---|
| D-12 | Baseline ignores stated order ("late brunch then something artsy") | `app/domain/fallback.py` (no ordering), `parser_rules.py` (not captured) | `holdout_koramangala_brunch`: an art gallery comes first at 11:00 and Carrots is at 12:59 | Capture "X then Y" order hints and add a soft ordering penalty |
| D-13 | Soft tensions (quiet vs live music) are never named in trade-offs | `fallback.py:460-478` | `holdout_quiet_vs_live_music` | Add a template trade-off when mood words conflict with an interest |
| D-14 | Trace never marks `get_weather` failed or recovered | `app/pipeline.py:43-50` (tool_completed only, no `degraded` event) | `weather_unavailable` eval (xfail) | Emit `degraded` (or `tool_failed` with `recovered: true`) when status is unavailable |
| D-16 | Template overclaims | `fallback.py:446,486-488` | Two cases (`test_summary_does_not_claim_food_checks_without_food_stops`, `test_calm_claim_is_backed_by_the_busy_windows`):<br>• "Every food stop has confirmed vegetarian options" when the plan has no food stops (late-night plan)<br>• "usually calm at this time" on Cubbon Park at 16:55–17:25, inside its own 17:00–18:30 busy window, and on every unknown-popularity stop | Claim calm only for low popularity outside busy windows; add the diet sentence only when food stops exist |
| D-17 | Weather check repeats the rain chance: "Mainly clear, high 28°C, 10% chance of rain; 10% chance of rain" | `app/domain/validator.py:486-487` | `test_weather_check_detail_is_not_duplicated` | Don't append `rain` when the summary already has it |
| D-18 | One malformed Google place kills the whole search, and the run fails with a 500 because the pipeline only catches `ProviderError` (`pipeline.py:81`) | `normalize.py:121`, `google_places.py:72-73,141,183` | `test_google_search_skips_a_malformed_place[*]`:<br>• non-string type → `AttributeError`<br>• `hour: null` → `TypeError`<br>• `"day": "SAT"` → `ValueError`<br>• negative `priceRange` → `ValidationError` | Wrap `place_to_candidate` per place (skip and log), and coerce defensively |
| D-19 | Google 24-hour places sent as per-day 00:00–24:00 periods are not joined across midnight, so a stop at 23:30–00:30 is HOURS_CONFLICT (false negative) | `google_places.py:124-151` (and `_current_hours` `:86-121`) | `test_google_24h_per_day_periods_cover_a_visit_across_midnight` | Apply the same next-day join `parse_osm_opening_hours` uses (`normalize.py:377-380`) |
| D-20 | JSON-LD price strings are mangled | `events_web.py:189-207` | Two cases (`test_jsonld_price_range_string_is_not_underestimated`):<br>• "₹1,500 - ₹2,000" → category default 0–1500, below the listed maximum<br>• "1e3" → 13 | Parse ranges explicitly; reject anything that is not plain numeric |
| D-21 | Constraint mapper false positives | `constraints.py:31,34,35` | `test_constraint_false_positives[*]`:<br>• "I'm not vegetarian" → `diet:vegetarian`<br>• "easily accessible by metro" → `access:wheelchair`<br>• "not busy this weekend" → `crowd:avoid` | Negation lookbehind for diet; require a mobility context for "accessible"; drop bare "not busy" |
| D-22 | Street food (`seated: false`) satisfies "include a sit-down meal" | `data/categories.yaml:39-50` (`meal: true`), `evidence.py:212-213` | `test_street_food_is_not_a_sit_down_meal` | `sit_down_meal` requires `meal and seated` |
| D-23 | Travel config chosen by city name only: "Delhi" in Canada gets INR auto fares, and currency-less locations get USD fares | `travel.py:114-120,135` | `test_travel_city_name_collision_uses_the_location_currency` | Key by country plus name; never return a fare currency different from the location's |
| D-24 | `mypy` not clean on Phase 1 files: `app/pipeline.py:72: Unused "type: ignore" comment` (AGENTS.md rule 7) | `app/pipeline.py:72` | `uv run mypy app/...` | Type `provider: PlacesProvider` and drop the ignore |
| D-25 | `PlanRequest.date` unbounded (see D-05) | `app/domain/schemas.py:70` | `PlanRequest(date=date(2020,1,4))` gives a complete plan for 2020 | Validator on the request model |
| D-26 | Minor provider hardening gaps | `events_web.py:525-531`, `normalize.py:256-257` | Code read:<br>• robots.txt is read in full before slicing to 200 kB (no streaming cap)<br>• `is_https_url` accepts `"https://"` with no host (an event can carry it; the validator rejects it later) | Stream robots.txt with a size cap; check `netloc` |
| D-27 | Events without `geo` are placed at the city centre and routed from there, and the `approx_location` tag is never surfaced | `events_web.py:329-333` | `event_to_candidate` on an event without `location.geo` gives `lat/lng` equal to the city centre | Surface "location approximate" on the stop, or exclude these from routed plans |

---

## 2. Command results

All commands were run from `backend/`.

| Command | Result |
|---|---|
| `uv run pytest --ignore=tests/graph --ignore=tests/llm` | **1014 passed, 7 deselected, 45 xfailed** (5.3 s). Of these, 889 are the existing tests (all pass) and 125 passed + 45 xfailed are the new verification tests. Without `tests/verification`: 889 passed, 7 deselected. |
| `uv run ruff check app tests --exclude app/graph --exclude app/llm` | Phase 1 scope: **All checks passed.** The literal command currently reports 3 errors, all in Phase 2 test files created during this run: `tests/graph/test_eval_cases_offline.py:131` (RUF059), `tests/graph/test_graph_mechanics.py:181` (RUF012) and `tests/llm/test_registry.py:107` (RUF043). Not Phase 1. |
| `uv run mypy app/domain app/providers app/pipeline.py app/baseline.py app/deps.py app/cli.py` | **1 error:** `app/pipeline.py:72: error: Unused "type: ignore" comment [unused-ignore]` (D-24). |
| `uv run ruff check tests/verification`; `uv run mypy --explicit-package-bases tests/verification` | Clean. The only mypy output is the same `pipeline.py:72` error. |

---

## 3. Eval cases on the no-LLM baseline

**How the cases were run:**

* Requests were built with the file's merge semantics, using `fixture_settings` and fixture weather per case.
* Provider faults came from `fault=`, and fixture events from `FixtureEvents(...)`.
* Sentinel API keys were set so that any leak would show up.
* Each plan was checked by `tests/verification/independent_checks.py`. It recomputes everything from
  `data/fixtures/*.json`, `data/snapshot/bangalore.json`, `categories.yaml` and `cities.yaml`:
  * legs, using its own implementation of haversine × 1.4, speeds, pickup and fares;
  * worst-case cost with `ceil(10%)`;
  * the schedule and elapsed time;
  * opening hours, with an "hours unverified" label for unknown hours;
  * diet, wheelchair and crowd evidence against the request text;
  * pacing, using `PROFILES`;
  * number-free prose;
  * distinct stops;
  * the 40 km radius;
  * sunset, using the clear-sky sunset when weather is unavailable;
  * rain Plan B: indoor, open, not a duplicate, and meeting the constraints;
  * trace order and secret sentinels.
* A self-test, `test_independent_checker_detects_injected_faults`, proves the checker catches injected faults.

| Case | Expected | Actual | Assertion failures |
|---|---|---|---|
| assignment_example | complete | complete | none (plan quality: D-09 thali at 15:00) |
| city_alias_bengaluru | complete | complete | none (same ID, timezone and currency as Bangalore) |
| other_indian_city | complete | complete | none (all stops within 40 km, no `fx:blr` IDs) |
| free_text_complete | complete | complete | none (all parse_expect fields match; vegan evidence; no question) |
| benign_ignore_phrase | complete | complete | none ("prefers low-cost" not hard-assertable; worst case ₹765) |
| high_energy_long_day | complete | complete | none (6 stops; lunch 12:17). Plan quality: D-09 second meal at 15:57, D-15 concert hall at 10:00 |
| tight_budget_free_activities | complete | complete | none (₹182 ≤ ₹300 including ₹45 fare; omission names the budget) |
| wheelchair_access | complete / no_feasible | complete | none (every stop wheelchair-confirmed; no decoys) |
| date_night_no_diet | complete | complete | none (dinner starts 21:06; no diet applied) |
| vague_free_text | needs_clarification | needs_clarification | none (city + budget_time with quick replies). Interrupt/thread ID is Phase 2 |
| clarification_answered | complete | needs_clarification → answers → complete | none (no re-ask even without `asked_before`) |
| missing_city_api | needs_clarification | needs_clarification | none (no place search ran) |
| currency_ambiguity_abroad | needs_clarification | needs_clarification | none, but answering "INR" leads to D-01 |
| unknown_city | needs_clarification | needs_clarification | none |
| contradictory_constraints | needs_clarification | needs_clarification | none (both mapped and named) |
| impossible_budget | no_feasible_plan | no_feasible_plan | none (₹495 cheapest budget verified exact: ₹495 feasible, ₹494 not) |
| late_night_start | complete / no_feasible | complete | none (single stop 23:00–01:00). D-16 summary overclaims |
| rain_forecast | complete | complete | none (all indoor; rain in summary and why) |
| weather_unavailable | complete | complete | **trace does not mark get_weather failed/recovered** (D-14, xfail) |
| event_grounding | complete | complete | none (no event used; only the valid one was offered) |
| places_provider_outage | complete / fallback | complete | none (all `curated_snapshot` with last_checked). D-15: 2 of 3 stops have unverified hours |
| places_outage_no_snapshot_city | no_feasible_plan | no_feasible_plan | none |
| event_search_failure | complete | complete | none |
| prompt_injection_in_mood | complete / needs_clarification | complete | none (no sentinel; budget enforced) |
| prompt_injection_in_event_data | complete | complete | none (description never in output) |
| negative_budget | rejected | rejected (`PlanRequest` ValidationError on `budget`) | none (HTTP 400 not tested: API layer) |
| holdout_koramangala_brunch | complete | complete | **brunch is not first** (art gallery at 11:00, D-12, xfail) |
| holdout_rainy_museum | complete | complete | none (outdoor park has an open indoor Plan B) |
| holdout_vague_short | needs_clarification | needs_clarification | none |
| holdout_jain_diet | complete / no_feasible | complete | none (Jain-confirmed) |
| holdout_small_budget_music | complete / no_feasible | complete | none |
| holdout_quiet_vs_live_music | complete | complete | **compromise not named** (D-13, xfail) |
| holdout_injection_variant | complete / needs_clarification | complete | none (budget 2000 enforced) |
| holdout_family_long_day | complete | complete | **no meal in a normal meal window** (17:56 start, D-09, xfail) |

**Skipped** (need an LLM fault or the HTTP/UI layer): model_timeout, provider_fallback_chain, model_refusal,
invalid_submission_then_repair_exhausted, agent_ends_without_submit, diet_dropped_by_parser, xss_in_input,
oversize_body, rate_limited, share_link_roundtrip.

---

## 4. Plan quality (demanding product review)

I ran 16 CLI variations on the frozen world: the brief in clear weather and in rain, Koramangala and Mumbai free
text, high, low and medium energy, vegan at 18:30, a 10-hour day with a partner, wheelchair in rain, outdoors-only,
₹300, ₹600, ₹5,000 and Mumbai at 11:00.

### What works

* Budgets, times and hours are always right.
* Diet and wheelchair evidence is respected.
* Rain plans go indoors and include a sensible Plan B (Freedom Park → NGMA, wheelchair-confirmed).
* Impossible requests name an exact cheapest budget.
* Event grounding is clean.
* The template prose has no numbers.

### What a reviewer would flag

1. **Meal timing (D-09).**
   * The brief's own example opens with an 80-minute thali at 3:00 PM for "tired but wants to do something fun".
     A café or dessert in the 16:00–18:00 snack window would fit better.
   * Mumbai pure-veg: restaurant at 3:00 PM.
   * High-energy day: two restaurant meals 2.5 hours apart.
   * Family day: the meal runs 17:56–19:46, labelled "a proper meal at a sensible time".
2. **Idle time and under-use.**
   * 10-hour partner day: 173 idle minutes before a 5:30 PM event (D-10).
   * Mumbai energetic, 8 h: ends at 4:19 PM with 2 h 41 m unused. This is mostly a small-fixture limitation once
     the CLI's inherited crowd-avoid default removes Gateway of India.
3. **Durations are stretched to category maxima.**
   * 90 minutes at a small gallery for a "lazy" user.
   * 150 minutes at an open-mic room.
   * 110-minute casual dinners.
   * 120 minutes at the Gateway of India at midday in 33°C heat.
   * `_stretch` (`fallback.py:372-389`) fills time without asking whether the length makes sense.
4. **Unknown-hours concert hall as a destination (D-15).** "Chowdiah Memorial Hall 10:00–11:30 AM" is not an
   activity unless there is a concert.
5. **The low-energy rule clashes with outdoor wishes (D-11).** "Chill + outdoors only" is reported as impossible,
   and the stated reason is wrong.
6. **Repetition and template tone.**
   * "Keeps the day moving with a lively restaurant / park / art gallery" repeats on every stop of high-energy
     plans.
   * "Coffee then more coffee" (Indian Coffee House → Matteo Coffea) for a 2-hour coffee request.
   * "One of the pricier picks" on a ₹40–120 café.
7. **Interests and mood.**
   * "Romantic" is not used at all: the date night picks Airlines Hotel over the "romantic" tagged options.
   * Order hints ("brunch then art", D-12) and tensions ("quiet" vs "live music", D-13) are ignored.
   * Interest coverage otherwise works.
8. **Late-night plan.** A single 2-hour stop in a 3-hour window, with a summary claiming confirmed vegetarian food
   stops that don't exist (D-16).

---

## 5. Data honesty and security review

### Data honesty

* `SOURCES.md` counts match the files: 41 snapshot places; 51 Bengaluru and 13 Mumbai fixture candidates; 4
  locations; 18 `_fictional`.
* Every fixture candidate carries "Synthetic test attributes".
* Every snapshot candidate carries "© OpenStreetMap contributors", an OSM URL and `last_checked` = 2026-11-02.
* Fixture events use `example.com`.
* The OSM diet and wheelchair mapping matches the documentation (`limited` → UNKNOWN; vegan yes → vegetarian
  CONFIRMED).
* Fixture sunsets match the real values for 7 Nov 2026 within a few minutes.
* Note: real venue names (Toit, Hard Rock Cafe, Rameshwaram Cafe, MTR) carry synthetic negative attributes such as
  "wheelchair: no" or "busy all day". This is labelled, but never show these on a public `/evals` page as facts.

### Security

* Keys travel only in headers.
* `GooglePlaces`, `TavilyEvents` and `Settings` reprs hide secrets.
* `ProviderError` messages contain the status code only (verified with a sentinel in the response body).
* No sentinel appears in any result or trace.
* Event pages:
  * are allow-listed;
  * must be https;
  * follow no redirects;
  * are robots-checked;
  * are capped at 1 MB and three pages.
* JSON-LD descriptions are never stored, and names are cleaned and truncated to 120 characters.
* Gaps: D-18, D-26.

---

## 6. What I could not verify

* **Live providers:** real Google Places, Open-Meteo and Tavily responses (no keys or network used), and the
  accuracy of fare tariffs in `cities.yaml` (news-sourced, unverified by design).
* **API and UI behaviour:** HTTP 400/413/429, XSS rendering, share links, and the LangGraph interrupt/thread ID.
  These belong to the API, UI and Phase 2.
* **LLM-dependent cases:** the 6 fault cases listed in §3.
* **Soft eval assertions** that need a judge: "Prefers free or low-cost stops" and "trade-off text explains" beyond
  keyword presence.
* **Behaviour outside the frozen world:** the Mumbai under-fill may not occur with real data.

## 7. Files added

* `backend/tests/verification/independent_checks.py`: independent recomputation library.
* `backend/tests/verification/test_phase1_baseline_evals.py`: 38 tests (34 eval cases, the negative-budget
  rejection, and stricter companions).
* `backend/tests/verification/test_phase1_adversarial.py`: 132 tests (validator, parser, providers, travel,
  end-to-end reproductions and plan quality).
* `docs/verification/PHASE1.md`: this report.

---

## 8. Re-verification after the D-01..D-27 fixes

I re-ran everything independently. Fixes were judged by behaviour: the original reproductions, the CLI
scenarios, all eval cases, and a new old-versus-new comparison. The comparison sent **1,500 seeded random
requests** through both the pre-fix code (`git archive HEAD`) and the fixed working tree, and ran every complete
plan through `tests/verification/independent_checks.py`. I also checked that the coordinator's removal of my
xfail marks left the assertions unchanged.

### 8.1 Verdict: not signed off

* 25 of the 27 defects are **fixed**, and 2 are **partially fixed** (D-07, D-22).
* The fixes introduced **1 P0 and 3 P1 regressions**:
  * **R-4 (P0):** diet constraints vanish when a negated diet word appears.
  * **R-1 (P1):** explicit "6am" becomes 6 PM.
  * **R-2 (P1):** "11.30 to 1.30" becomes a 12-hour window.
  * **R-3 (P1):** the new meal-window rule drops evening and late-morning plans and gives misleading reasons.
* Once those four are fixed, Phase 1 can be signed off. The remaining items are P2.

### 8.2 Command results (from `backend/`)

| Command | Result |
|---|---|
| `uv run pytest --ignore=tests/graph --ignore=tests/llm` | **1266 passed, 7 deselected, 20 xfailed.** Existing tests: 1078 passed. Verification: 188 passed, plus 20 strict xfails, all in `test_phase1_reverify.py`. |
| `uv run ruff check app tests --exclude app/graph --exclude app/llm --exclude tests/graph --exclude tests/llm` | All checks passed. |
| `uv run mypy app/domain app/providers app/pipeline.py app/baseline.py app/deps.py app/cli.py` | Success: no issues found in 29 source files (D-24 fixed). |

### 8.3 Status per defect

| ID | Status | Evidence |
|---|---|---|
| D-01 | **fixed** | CURRENCY_MISMATCH is raised for stops, legs and Plan B. Budgets are converted with labelled reference rates (`data/fx.yaml`, 2026-11-02):<br>• London + "INR" turns ₹20,000 into £156; the plan is GBP-only, with the assumption "₹20,000 ≈ £156 at an approximate reference rate"<br>• $30 in Bangalore becomes ₹2,850<br>No false positives on INR data: 0 currency exclusions across the fixture, snapshot and event pools for Bangalore and Mumbai. Residual: R-8. |
| D-02 | **fixed** | "vegan and jain" gives `[diet:vegan, diet:jain]`. A vegan-only restaurant gives DIET_UNVERIFIED (Jain). |
| D-03 | **fixed** | The FoodEvent "Kebab and Grill Dinner Night" under vegetarian gives DIET_UNVERIFIED. The summary only claims diet checks when food stops exist. |
| D-04 | **fixed** (residual R-9) | When weather is unavailable, the computed sunset is filled in. It is within 5 minutes of the fixture for all 4 cities. Mumbai at 19:30 no longer gets Marine Drive, and the validator flags DAYLIGHT_CONFLICT. Grid: 0 night walks (pre-fix code: 22). |
| D-05 | **fixed** (residual R-5) | When it is Saturday 18:00, the plan starts at 18:15. A past date (2020-01-04) is replaced by the coming Saturday, with an assumption. |
| D-06 | **fixed** | Closed, duplicate or over-budget Plan Bs get PLAN_B_INVALID. The planner only picks open, distinct, affordable ones. Grid: 0 Plan B or rain failures (pre-fix code: 5). |
| D-07 | **partially fixed / regressed** | Fixed:<br>• "2500 budget/max", "with 2500", "4.30pm", "4.30 to 8.30 pm" and "4.30-6.30" all parse<br>• "Bangalore, 600 budget…" now plans within ₹600 (worst case ₹352)<br>Residual: "spend like 600 or so" is only disclosed ("Couldn't tell whether “600” is your budget, so it wasn't used"), and ₹1,500 still applies (R-12).<br>Regressions: R-1, R-2. |
| D-08 | **fixed** (new false positive R-7) | All 8 missed phrases are now flagged. |
| D-09 | **fixed for the brief, regressed elsewhere** | No 3 PM thali. Meals start inside their windows (dinner 19:30+, lunch 12:00+). Regression: R-3. |
| D-10 | **fixed** | Waits are capped at 30 minutes, and idle time is excluded from time use. Side effect: the 10-hour partner day now ends at 3:15 PM (see R-11). |
| D-11 | **fixed** | "chill + outdoors only" returns `complete` with a PACING_RELAXED warning. Infeasible reasons now name pacing rules ("A low-energy day needs at least one seated stop…"). Weak plan: one 45-minute stop in a 4-hour window (R-11). |
| D-12 | **fixed** (P2 residual) | Koramangala: Dyu Art Cafe brunch at 11:00, then Studio Kora ("brunch first, as you asked"). An order the meal windows can't satisfy is silently dropped: "dinner then live music, 4–9pm" puts dinner last with no trade-off. |
| D-13 | **fixed** | Trade-off: "Live music is never silent, but this is one of the calmer rooms…". |
| D-14 | **fixed** | `tool_failed` for get_weather with `{"code": "weather_unavailable", "recovered": true}`. |
| D-15 | **fixed in the planner** | Programme venues (concert halls, theatres, cinemas) with unknown hours are excluded, with an omission reason. The validator does not enforce this, so a Phase 2 agent could still book one (HOURS_UNVERIFIED warning only). |
| D-16 | **fixed** | Calm claims are backed (`calm_at`), and food-stop claims only appear with food stops. |
| D-17 | **fixed** | "Mainly clear, high 33°C, 5% chance of rain". |
| D-18 | **fixed** | Malformed places raise `MalformedPlace` and are skipped. `get()` wraps the error as ProviderError. |
| D-19 | **fixed** | Per-day 24-hour periods become `(0, 2880)`. |
| D-20 | **fixed** | "₹1,500 - ₹2,000" gives 1500–2000 (ticket price). "1e309" falls back to the category default. |
| D-21 | **fixed for the 3 phrases, regressed** | "accessible by metro" and "not busy" no longer map, and "I'm not vegetarian" no longer adds a diet. Regressions: R-4 (P0) and R-6. |
| D-22 | **partially fixed** | Street food no longer satisfies a sit-down meal. Residual R-10: the check is category-only, so a standing-only restaurant (`seated: false`) still counts. In the grid, it was the only independent-check failure (19 plans). |
| D-23 | **fixed** | City lookup is country-aware: "Delhi" in Canada gets CAD cab fares. |
| D-24 | **fixed** | mypy is clean. |
| D-25 | **fixed** | Dates in the past or more than 60 days ahead are replaced, with an assumption. |
| D-26 | **fixed** | robots.txt is streamed with a 64 KB cap and a wall-clock deadline. `https://` URLs without a host are rejected. |
| D-27 | **fixed** | Event address "Location approximate - check the venue", LOCATION_APPROXIMATE warning, and a scoring penalty. |

**Grid comparison** (1,500 seeded requests, both code versions):

| | Pre-fix | Fixed |
|---|---|---|
| Complete plans | 1310 | 1266 |
| Independent-check failures | 43 (night walks without weather, rain without Plan B, closed Plan B, standing "sit-down" meals) | 19, all R-10 |
| Lost plans (complete → no_feasible) | — | 44, none gained |

34 of the 44 lost plans start at 19:00. The 19:00 infeasible rate doubled, from 18% to 36% (R-3). Some losses are
correct: night walks are no longer allowed.

### 8.4 New defects found in re-verification

All have strict-xfail reproductions in `backend/tests/verification/test_phase1_reverify.py`.

**R-4 (P0, regression from D-21): a negated diet word removes every diet constraint.**

* **Where:** `app/domain/constraints.py:59-62`. `negated_veg` skips *all* diet rules if any of
  `not|never|non` + `veg|vegetarian|vegan|jain` appears anywhere in the text.
* **Phrases now lost:**
  * `map_constraint_text("no non-veg")` gives `[]`, though it was `diet:vegetarian` before the fix. This is the
    most common Indian way to say vegetarian.
  * "pure veg please, no non-veg places" gives `[]`.
  * "strictly vegetarian, never non veg" gives `[]`.
  * "I'm vegetarian, not vegan" gives `[]`.
  * "not a vegetarian place please, I'm vegan" gives `[]` (vegan lost).
* **End to end:** `constraints=["no non-veg"]` returns a clarification question. Answering the offered quick reply
  "Keep it as a preference" produces a date-night plan with **The Malabar Table (vegetarian: NO, seafood)**.
  CONSTRAINT_DROPPED cannot catch this, because `original_constraints` uses the same mapper.
* **Tests:** `test_r4_negated_diet_words_keep_the_real_diet[*]`, `test_r4_no_non_veg_never_yields_a_non_veg_restaurant`.
* **Fix:**
  * Scope negation to the matched span: only suppress the diet word that is itself negated.
  * Keep `no (meat|non-veg)` as vegetarian.
  * A negation must never remove a diet that is stated positively elsewhere in the same text.

**R-1 (P1, regression): explicit morning times are moved to the evening.**

* **Where:** `app/domain/parser_rules.py:501-512`, `_EARLY_HINT` / `_extract_time`. `\bam\b` does not match "6am"
  (there's no word boundary between the digit and "am").
* **Effect:**
  * "from 6am for 3 hours" starts at 18:00.
  * "5am to 9am" becomes 17:00–21:00.
  * "from 12am to 3am" starts at noon.
* **Tests:** `test_r1_*`.
* **Fix:** decide on the raw am/pm tokens, e.g. `\d\s*a\.?m\b`, and shift only when no am/pm was given anywhere.

**R-2 (P1, existing defect newly exposed by the dotted-time fix): ranges that cross noon without am/pm become
12-hour windows.**

* **Where:** `app/domain/timeutil.py:127`, `parse_time_range`. When `end <= start`, it adds 1440, and the result is
  clamped to 720 minutes.
* **Effect:**
  * "free 11.30 to 1.30" gives 11:30 for 720 minutes, instead of 11:30–13:30.
  * "9.30 to 1" gives 9:30 for 720 minutes.
  * The colon form ("11:30 to 1:30") was already wrong, but the common dotted form used to be ignored and now hits
    this path.
* **Tests:** `test_r2_ranges_across_noon[*]`.
* **Fix:** with no am/pm on either side, prefer `end + 720` when that gives a positive span of 12 hours or less.

**R-3 (P1, regression from D-09): plans that start shortly before a meal window are lost, with misleading
reasons.**

* **Where:** `app/domain/fallback.py:406-412`, `_extend`. A full meal must *start inside* breakfast, lunch or
  dinner; `main_meal_window` (`app/domain/evidence.py:108`) defines dinner as 19:30–22:00. The first stop cannot wait for the window to open, and
  `app/domain/infeasible.py` never tests meal timing.
* **Effects:**
  * **19:00, walks/nature, vegetarian:** previously a dinner at 19:00. Now `no_feasible_plan`: "Even with an
    unlimited budget and a longer window, nothing found fits this start time… Requiring confirmed vegetarian food
    rules out 1 of 15 places", and no relaxation is offered.
  * **11:00, wheelchair, food/music/walks:** previously a plan with a wheelchair-confirmed tiffin room. Now the
    *only* one-tap fix offered is **"Drop 'wheelchair accessible'"**.
  * **"4 to 8pm, music first, then dinner":** no dinner at all.
* **Tests:** `test_r3_*`.
* **Fix:**
  * Accept a meal that starts up to about 30 minutes before a window with at least half of it inside, or allow a
    short initial wait.
  * Add a meal-timing relaxation to `explain_infeasible`.
  * Never offer to drop a diet or accessibility constraint when timing is what binds.

**R-5 (P2): the Saturday start shift leaves a contradictory assumption.**

* **Where:** `app/pipeline.py`, `build_context`. It appends "Starting at 6:15 PM because…" without removing
  "Starting at 3:00 PM".
* **Test:** `test_r5_shifted_start_has_a_single_start_assumption`.

**R-6 (P2): "I'm not vegetarian" now blocks with a clarification question.** `_HARD_RE` matches "not", and nothing
maps the phrase. Test: `test_r6_not_vegetarian_is_not_a_blocking_question`.

**R-7 (P2): venue names trip the prose check.**

* **Where:** `app/domain/validator.py`, `_PROSE_PATTERNS` (clock time now includes `noon|midday|midnight`).
* **Effect:** "Midnight Scoops is a sweet, relaxed way to end the day." gets PROSE_NUMERIC_CLAIM, and the fixture
  contains "Midnight Scoops Dessert Bar". Phase 2 agents would be sent into repair loops.
* **Test:** `test_r7_venue_names_are_not_clock_times`.
* **Fix:** blank out ledger names before checking the prose.

**R-8 (P2): price-filtered searches are reported as "No places or events were found for London".**

* **Where:** `app/domain/infeasible.py:117`, the `if not unique` branch. It is reached when `default_queries` sets
  `max_price_level` for a small converted budget (₹2,000 becomes £15).
* **Test:** `test_r8_price_filtered_search_is_not_reported_as_no_places`.
* **Fix:** say that nothing affordable was found, and offer a budget relaxation.

**R-9 (P2): the computed sunset bypasses the safety margin.**

* **Where:** `app/pipeline.py:75`, `build_context`. It writes the raw `sun_times` sunset into
  `weather.sunset_min`, so `effective_sunset`'s documented 15-minute margin never applies. The computed values
  run up to 5 minutes later than the fixture (Jaipur 1061 vs 1056).
* **Polar night:** `sun_times` returns None, so there is no darkness check at all.
* **Test:** `test_r9_computed_sunset_keeps_the_safety_margin`.

**R-10 (P2, residual of D-22): a standing-only restaurant satisfies "include a sit-down meal".**

* **Where:** `app/domain/evidence.py:279-280`, `satisfies_include`. It uses the category's `seated`, not the candidate's.
* **Effect:** The Rameshwaram Cafe (`seated: false`) is used as the sit-down meal in 19 grid plans.
* **Test:** `test_r10_standing_only_restaurant_is_not_a_sit_down_meal`.

**R-11 (P2, plan quality):**

* **Under-filled windows:**
  * "free from 4.30pm to 8pm, love coffee" gives one 90-minute café, using 43% of the window. A park plus café
    plan scores lower because `_quality` caps the time-use bonus at 60%.
  * The 10-hour low-energy partner day ends at 3:15 PM, because of the 3-stop cap.
  * "Chill + outdoors only" gets a single 45-minute stop.
* **Weaker rain plan:** the brief's rain plan now includes an outdoor kids' park (Jawahar Bal Bhavan, with a Plan
  B) where it used to be fully indoor. The park also appears in most adult low-energy plans.
* **Jain diet:** `holdout_jain_diet` now has no food stop at all, because the only Jain restaurant can't be used
  before 19:30, and no trade-off says so.

**R-12 (P2, residual of D-07): an unread amount is disclosed but not asked about.**

* **Where:** `parser_rules.py`, `parse_rules` / `apply_defaults`.
* **Effect:** "spend like 600 or so" still plans with the ₹1,500 default.
* **Fix:** ask a budget question when an unread amount of at least 50 is present.

### 8.5 Other observations (not defects)

* `PlaceQuery.neighbourhood` was added to the shared `schemas.py` contract. `data/fx.yaml` is new, labelled
  "approximate reference rates, not live", with sources and a date. Both are coordinator-owned changes.
* `app/domain/sun.py` carries a note that it was accidentally overwritten and rebuilt. It is now covered by
  `tests/unit/test_sun.py`, and its sunsets match the fixture within 5 minutes.
* All 34 applicable eval cases now pass every independent assertion, including the 4 that previously failed.

### 8.6 Files added in re-verification

* `backend/tests/verification/test_phase1_reverify.py`: 38 tests.
  * 20 strict xfails covering R-1 to R-10.
  * 18 passing tests that pin fixed behaviour, including a 300-request random grid in which every complete plan
    must pass the independent checker.
