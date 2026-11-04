# Additional venue requirements

The existing diet, entrance-accessibility, crowd, setting and inclusion rules are unchanged. The planner can also research eight specific facility types: private changing rooms, baby-changing facilities, wheelchair-accessible toilets, on-site parking, free parking, lifts/elevators, nursing rooms and lockers.

This is conservative evidence matching, not a claim to verify every possible natural-language requirement. Other mandatory requirements still ask for clarification. Rephrasing an unsupported requirement does not automatically make it supported. After the single clarification round, a still-unsupported mandatory requirement stops the plan. Choosing **Keep it as a preference** explicitly makes that clause optional; **Skip it** removes it.

## Data flow

1. Rule-based extraction reads user input, independently of the LLM, and creates stable requirement IDs. Requirements apply to all stops by default, to every food stop when stated, or to at least one stop when stated. No source lookup occurs for ordinary requests without these facilities.
2. Google Places supplies the venue's website URI alongside the existing place record. The application never follows arbitrary venue URLs itself. When `TAVILY_API_KEY` and the HTTP client are available, bounded Tavily searches request raw content from that website's exact domain.
3. Confirmation requires a raw official-domain page establishing the venue name and city and an explicit facility statement. A search snippet, unknown value, question, future promise, qualified claim or conflicting statement cannot confirm it. Source URL, exact quote and retrieval time accompany the assessment. Status is **confirmed**, **contradicted** or **unknown**. Web text is not passed to an LLM as instructions.
4. The server-owned candidate ledger carries these assessments. The model can select candidate IDs but cannot submit its own evidence records. The deterministic validator independently rechecks the source domain, quote and freshness. Both planners and every rain-alternative combination enforce mandatory facilities. An unknown or contradicted mandatory facility cannot produce a valid plan.
5. If no verified plan fits, the UI explains the verification limit and offers a specific **Make ... optional (not guaranteed)** action. This changes only that requirement. Optional facilities retain their unknown state and produce warnings rather than assurances. Cards, copied/WhatsApp text and calendar descriptions preserve the facility evidence/status.

## Operational limits

- At most five additional requirements, six searches and six seconds of facility research per run, within the existing overall deadline. Each search has at most 2.5 seconds and three results; responses are capped at 150 KB.
- Positive and negative evidence may be cached in memory for six hours (maximum 128 entries). Unknown is not cached. Cache keys include the candidate, official website, city and requirement IDs. A five-minute allowance accommodates retrieval after the run's context timestamp; older-than-six-hours or substantially future evidence cannot pass.
- HTTP failures, quota errors, timeouts, malformed/oversized responses and missing keys become **unknown**, never confirmation. A Tavily key enables research; it does not guarantee that every venue has usable public evidence.
- No new package or database migration is required. The new schema fields have defaults, so old candidate/preference/share records still load. Google website display content is redacted and rehydrated under the existing sharing policy.
- Website statements can be outdated or refer incompletely to a branch. Confirmation here means a matching official-source statement, not a guarantee of facilities on arrival. The UI asks users to confirm current availability directly.

## Reproduce without paid models

From `backend/`:

```bash
uv run pytest tests/unit/test_venue_requirements.py tests/providers/test_requirements_web.py tests/graph/test_facility_requirement_flows.py
uv run pytest
uv run ruff check app tests
uv run mypy app
```

From `frontend/`: `npm run lint`, `npm test`, `npm run e2e`.

All added backend tests use synthetic venues and mocked HTTP or fixture providers; they do not contact Tavily, Google, Cerebras or a local model. This feature adds evidence verification tools to the existing application harness; it does not change the production model.
