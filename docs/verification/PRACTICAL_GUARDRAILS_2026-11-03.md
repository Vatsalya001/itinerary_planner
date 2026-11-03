# Practical guardrails release — 3 November 2026

## Why this release exists

The prior Mumbai and London runs completed in AI mode and passed internal checks, but manual review found important real-world gaps. MuSo's official booking page excludes grown-ups visiting alone; Mumbai auto-rickshaw estimates ignored island-city restrictions; entrance accessibility was described too broadly; walking routes linked to driving directions; generic museum prices were not clearly distinguished from admission facts. An earlier smoke pass established orchestration, not full practical validity. Historical smoke evidence is retained, not rewritten as comprehensive verification.

## Changes and enforcement

- **Admission:** a narrow, dated official-policy overlay in `backend/data/venue_policies.json` blocks MuSo when age/accompaniment eligibility cannot be established. Clearly child-focused museums are conservatively excluded by name/curated audience tag too. Search tools filter these records; fallback prefilters and final validation independently reject them, including rain alternatives. No model prompt can bypass final admission validation. The current schema does not represent child ages, so this intentionally limits family recommendations rather than guessing.
- **Mumbai transport:** the declared city configuration uses cab estimates for motor trips city-wide, including suburbs, until route-zone validation exists. This is conservative; it does not assert a city-wide auto ban. Cab costs, pickup and travel bounds are recomputed. Independent test/eval arithmetic reads the declared configuration and remains separately implemented. A feasible beam-search regression changed from 182 to 190 minutes because the old window relied on auto estimates; its assertions were retained. Direct cab-mode regressions cover island-city and suburban trips.
- **Accessibility:** confirmed evidence remains entrance-only. Missing/negative entrance evidence still cannot satisfy a wheelchair requirement. Checks now mark wider accessibility as unverified; each requested stop shows entrance scope and a source link. Toilets, interiors, crossings/gradients and route or vehicle accessibility are not assumed. The transfer row uses an outdoor-transfer label for wheelchair requests and says timing is not a wheelchair-route estimate. Caveats are preserved in copied text and calendar events.
- **Directions:** backend URLs follow actual uniform leg modes. UI/copy links are rebuilt from leg modes, correcting stale driving URLs in old shared records too. Mixed-mode plans omit the misleading single-mode full-route link and provide per-leg directions instead. Maps links themselves do not establish wheelchair accessibility.
- **Pricing:** National Gallery general admission is recorded as free from its official page, with optional paid exhibitions excluded and an official policy link. A same-named unrelated venue/category does not inherit that policy. Generic admission bands produce provisional budget checks and ticket-price warnings. A missing zero category default is not treated as verified free admission; it displays unknown/not included in total. The summary says worst-case estimate and within estimated budget. Plan B pricing is also checked for uncertainty.
- **Personalisation:** short-travel requests carry a stronger travel penalty in candidate-option/fallback ranking. Transfers whose upper estimate exceeds 20 minutes receive an explicit compromise warning. Model guidance encourages nearby clusters. Existing deterministic explanations about budget and quiet-versus-music compromises are preserved.

## Verified before push

- Final full backend suite: **1,863 passed, 11 live/Ollama deselected**.
- Full frontend suite: **142 passed**.
- Chromium end-to-end suite: **21 passed**, including anonymous sharing, mobile layout, clarification and failure handling.
- Ruff passed; mypy passed for **74 source files**; frontend typecheck/lint and production build passed.
- Offline workflow/fault evaluation: **32/32**.
- Deterministic model-suite evaluation: **26/26** after restoring deterministic trade-off explanations. An intermediate 24/26 evaluation caught the explanation regression and was fixed before release.
- No hosted model calls were used in these local checks. Protected schema, provider protocol, settings and dependency files were not changed.

## Live acceptance

After deployment, check the health commit, submit fresh Mumbai and London prompts, inspect the trace and share pages, and record actual results separately. A saved plan created before this release is a historical snapshot; its admissions, fares and stored checks are not recomputed merely by opening it. Generate a new plan to apply these guardrails.

## Limits

This is a bounded assignment solution, not a guarantee of zero bugs. The policy overlay covers specific sourced facts; it is not comprehensive admission/age/membership verification for every place. Policies and prices need maintenance. Unknown prices are not guaranteed by contingency arithmetic. Travel is heuristic, not live route navigation. The app does not claim fully wheelchair-accessible routes. A short-travel preference is ranked and explained, not guaranteed as a hard limit. Cerebras quotas and the application's five-call/minute limiter remain unchanged. One or two successful live runs cannot establish sustained reliability.
