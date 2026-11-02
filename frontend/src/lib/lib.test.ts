// @vitest-environment node
import { describe, expect, it } from 'vitest'
import complete from '../test/fixtures/plan_complete.json'
import traceEvents from '../test/fixtures/trace_events.json'
import type { CanonicalPlan, PlanResult, TraceEvent } from '../types'
import {
  DEFAULT_FREE_TEXT,
  buildRequest,
  defaultFormState,
  patchForm,
  restoreConstraint,
  toggleConstraint,
  validateForm,
} from './form'
import { classifyDegradation, omissionLabel } from './labels'
import { applyPatch, legAfter } from './plan'
import { planToText, whatsappUrl } from './share'
import { humanize } from './format'
import { initialTrace, nodeLabel, reduceTrace, toolFailedLabel, toolLabel } from './trace'
import { mapsUrlFor, safeHttpsUrl } from './urls'

const plan = (complete as unknown as PlanResult).plan as CanonicalPlan
const events = traceEvents as unknown as TraceEvent[]

describe('trace reducer', () => {
  const state = events.reduce(reduceTrace, initialTrace)

  it('pairs started/completed events and keeps durations, reasons and summaries', () => {
    const parse = state.steps.find((s) => s.name === 'parse_preferences')
    expect(parse).toMatchObject({ status: 'done', durationMs: 1200, label: 'Read what you asked for' })
    const search = state.steps.find((s) => s.name === 'search_places')
    expect(search).toMatchObject({
      status: 'done',
      durationMs: 612,
      reason: 'Need a quiet vegetarian café for a seated first stop',
      summary: '8 places, 5 with vegetarian evidence, 2 likely busy',
      label: 'Searched for places',
    })
    expect(state.steps.filter((s) => s.name === 'search_places')).toHaveLength(1)
  })

  it('records failures, degradations, validation outcomes and the result', () => {
    expect(state.steps.find((s) => s.name === 'search_events')).toMatchObject({
      status: 'failed',
      label: 'Event search failed',
      reason: 'Look for seated live music on Saturday',
    })
    expect(state.degraded).toEqual([{ text: "Couldn't check events", kind: 'data' }])
    const validations = state.steps.filter((s) => s.kind === 'validation')
    expect(validations.map((v) => v.status)).toEqual(['warn', 'done'])
    expect(validations[0].details).toEqual(['Worst case is above the budget'])
    expect(state.steps.at(-1)).toMatchObject({ kind: 'result', label: 'Plan ready' })
    expect(state.finished).toBe(true)
  })

  const ev = (over: Partial<TraceEvent>): TraceEvent => ({ run_id: 'r2', seq: 0, ts: '2026-11-05T04:30:00Z', ...over }) as TraceEvent

  it("shows an agent's note only for the model's own tool calls (node tools)", () => {
    const s = [
      ev({ type: 'tool_called', seq: 1, node: 'gather_candidates', tool: 'search_places', reason: 'quiet vegetarian cafe' }),
      ev({ type: 'tool_completed', seq: 2, node: 'gather_candidates', tool: 'search_places', summary: '6 places' }),
      ev({ type: 'tool_called', seq: 3, node: 'get_weather', tool: 'get_weather', summary: 'Forecast for 2026-11-07' }),
      ev({ type: 'tool_called', seq: 4, node: 'tools', tool: 'check_plan', reason: 'Check the draft fits the budget' }),
    ].reduce(reduceTrace, initialTrace)
    const [code, weather, model] = s.steps
    expect(code).toMatchObject({ label: 'Searched for places', reason: null, summary: '6 places', node: 'gather_candidates' })
    expect(weather).toMatchObject({ label: "Checking Saturday's weather", reason: null, summary: 'Forecast for 2026-11-07' })
    expect(model).toMatchObject({ reason: 'Check the draft fits the budget', node: 'tools' })
  })

  it('labels the real backend nodes in plain language', () => {
    const s = ['build_context', 'resolve_location', 'fallback_planner', 'explain_infeasible']
      .flatMap((node, i) => [
        ev({ type: 'node_started', seq: i * 2 + 1, node }),
        ev({ type: 'node_completed', seq: i * 2 + 2, node, duration_ms: 5 }),
      ])
      .reduce(reduceTrace, initialTrace)
    expect(s.steps.map((x) => x.label)).toEqual([
      'Set up the day: weather, time window and pacing',
      'Found the city',
      'Built a backup plan without the AI model',
      'Worked out what blocked a plan',
    ])
  })

  it('has a plain-language label for every graph node and every emitted tool, and none for non-nodes', () => {
    // backend/app/graph/build.py add_node(...) names; "tools" is hidden because its tool events say more.
    const graphNodes = [
      'guard_input',
      'parse_preferences',
      'resolve_location',
      'check_clarity',
      'ask_clarification',
      'build_context',
      'plan_agent',
      'validate_plan',
      'fallback_planner',
      'explain_infeasible',
      'finalize',
    ]
    for (const node of graphNodes) {
      expect(nodeLabel(node), node).not.toBe(humanize(node))
      expect(nodeLabel(node, true), node).not.toBe(humanize(node))
    }
    // Model tools (app/graph/tools.py TOOL_NAMES) and code-run lookups (app/pipeline.py).
    const tools = ['search_places', 'search_events', 'estimate_travel', 'check_plan', 'submit_plan', 'resolve_location', 'get_weather']
    for (const tool of tools) {
      expect(toolLabel(tool), tool).not.toBe(humanize(tool))
      expect(toolLabel(tool, true), tool).not.toBe(humanize(tool))
      expect(toolFailedLabel(tool), tool).not.toBe(`${humanize(tool)} failed`)
    }
    // get_weather and gather_candidates only ever appear as tool or `node` fields of tool/degraded events, never as
    // node_started/node_completed, so they have no node label (a stray one would still read sensibly).
    expect(nodeLabel('get_weather')).toBe('Get weather')
    expect(nodeLabel('gather_candidates')).toBe('Gather candidates')
    expect(toolLabel('get_weather')).toBe("Checked Saturday's weather")
  })

  it('never repeats an agent turn number', () => {
    const s = [
      ev({ type: 'node_started', seq: 1, node: 'plan_agent' }),
      ev({ type: 'node_completed', seq: 2, node: 'plan_agent', summary: 'turn 4: check_plan', data: { turn: 4 } }),
      ev({ type: 'node_started', seq: 3, node: 'plan_agent' }),
      ev({
        type: 'node_completed',
        seq: 4,
        node: 'plan_agent',
        summary: 'turn 4: stopped (Not enough time left for the AI planner; used the deterministic planner)',
        data: { turn: 4 },
      }),
    ].reduce(reduceTrace, initialTrace)
    expect(s.steps.map((x) => [x.label, x.summary])).toEqual([
      ['Agent turn 4', 'check_plan'],
      ['Agent stopped', 'Not enough time left for the AI planner; used the deterministic planner'],
    ])
    expect(s.steps.filter((x) => /turn 4/.test(`${x.label} ${x.summary}`))).toHaveLength(1)
  })

  it('classifies degradations: data versus AI steps replaced by code', () => {
    const s = [
      ev({ type: 'degraded', seq: 1, node: 'plan_agent', summary: 'The AI planner timed out; used the deterministic planner' }),
      ev({ type: 'degraded', seq: 2, node: 'get_weather', summary: 'Weather not checked; planning without a forecast' }),
    ].reduce(reduceTrace, initialTrace)
    expect(s.degraded.map((d) => d.kind)).toEqual(['planner', 'data'])
    expect(classifyDegradation('Live place data unavailable; using the curated OpenStreetMap snapshot')).toBe('data')
    expect(classifyDegradation('Live place data is temporarily unavailable')).toBe('data')
    expect(classifyDegradation("Couldn't check this week's events")).toBe('data')
    expect(classifyDegradation('AI parser unavailable; used rule-based parsing')).toBe('parser')
    expect(classifyDegradation("The AI planner didn't submit a plan; used the deterministic planner")).toBe('planner')
    expect(classifyDegradation('The AI model declined this request; used the deterministic planner')).toBe('planner')
    expect(classifyDegradation('The primary AI model (a) failed; switched to b')).toBe('model')
  })

  it('ignores duplicate deliveries', () => {
    const again = reduceTrace(state, events[3])
    expect(again).toBe(state)
    expect(events.every((e) => e.type !== 'heartbeat')).toBe(true) // the server now sends ": keep-alive" comments
  })

  it('tolerates legacy heartbeat events from older servers (no step, no dedup entry)', () => {
    const legacy: TraceEvent = { type: 'heartbeat', run_id: 'r', seq: 0, ts: '2026-11-05T04:31:00Z' }
    const once = reduceTrace(state, legacy)
    const twice = reduceTrace(once, legacy)
    expect(once.steps).toBe(state.steps)
    expect(twice.steps).toBe(state.steps)
    expect(twice.seen).toBe(state.seen)
    expect(twice.lastEventAt).toBe('2026-11-05T04:31:00Z')
  })
})

describe('request building', () => {
  it('sends only the description when the form is untouched', () => {
    expect(buildRequest(defaultFormState(), {})).toEqual({ free_text: DEFAULT_FREE_TEXT })
  })

  it("sends the brief's fields when the description is empty", () => {
    const form = { ...defaultFormState(), freeText: '' }
    expect(buildRequest(form, {})).toEqual({
      city: 'Bangalore',
      budget: 2000,
      available_time: 240,
      mood: 'tired but wants to do something fun',
      interests: ['food', 'music', 'walks'],
      constraints: ['vegetarian', 'avoid crowded places'],
    })
  })

  it('adds edited fields on top of the description', () => {
    const form = { ...defaultFormState(), budget: '1500', startTime: '11:00' }
    expect(buildRequest(form, { budget: true, startTime: true })).toEqual({
      free_text: DEFAULT_FREE_TEXT,
      budget: 1500,
      start_time: '11:00',
    })
  })

  it('validates bounds', () => {
    expect(validateForm(defaultFormState())).toEqual({})
    expect(validateForm({ ...defaultFormState(), budget: '-5' }).budget).toBeTruthy()
    expect(validateForm({ ...defaultFormState(), groupSize: '0' }).groupSize).toBeTruthy()
    expect(validateForm({ ...defaultFormState(), freeText: '', city: '' }).form).toBeTruthy()
  })

  it('groups the budget limit for the chosen currency, like fmtMoney', () => {
    const over = (currency: string) => validateForm({ ...defaultFormState(), currency, budget: '2000000' }).budget
    expect(over('INR')).toBe('Enter a budget between ₹0 and ₹10,00,000.')
    expect(over('USD')).toBe('Enter a budget between $0 and $1,000,000.')
    expect(over('GBP')).toBe('Enter a budget between £0 and £1,000,000.')
    expect(over('EUR')).toBe('Enter a budget between €0 and €1,000,000.')
    expect(over('AED')).toBe('Enter a budget between AED 0 and AED 1,000,000.')
    // "From city": the currency is not known yet, so no symbol and no Indian grouping.
    expect(over('')).toBe('Enter a budget between 0 and 1,000,000.')
    expect(validateForm({ ...defaultFormState(), currency: 'USD', budget: '-1' }).budget).toBe(
      'Enter a budget between $0 and $1,000,000.',
    )
  })

  it('unions relaxed_constraints across relaxations; other keys replace (D-P2-02)', () => {
    const first = applyPatch({ free_text: 'x', constraints: ['vegan'] }, { relaxed_constraints: ['access:wheelchair'] })
    expect(first).toEqual({ free_text: 'x', constraints: ['vegan'], relaxed_constraints: ['access:wheelchair'] })
    const second = applyPatch(first, { relaxed_constraints: ['diet:jain', 'access:wheelchair'] })
    expect(second.relaxed_constraints).toEqual(['access:wheelchair', 'diet:jain'])
    expect(applyPatch(second, { constraints: ['vegetarian'] }).constraints).toEqual(['vegetarian']) // replaced
    expect(applyPatch(second, { relaxed_constraints: null }).relaxed_constraints).toBeUndefined()
    expect(applyPatch({ free_text: 'x' }, { relaxed_constraints: [] })).toEqual({ free_text: 'x' })
  })

  it('remembers dropped constraints in the form so later plans keep them dropped', () => {
    const start = defaultFormState()
    start.toggles.wheelchair = true
    const { form, dirty } = patchForm(start, {}, { relaxed_constraints: ['access:wheelchair'] })
    expect(form.relaxed).toEqual(['access:wheelchair'])
    expect(form.toggles.wheelchair).toBe(false)
    expect(dirty).toEqual({}) // not an edit: the description stays authoritative
    expect(buildRequest(form, dirty)).toEqual({ free_text: DEFAULT_FREE_TEXT, relaxed_constraints: ['access:wheelchair'] })

    const twice = patchForm(form, dirty, { relaxed_constraints: ['diet:vegetarian'] }).form
    expect(twice.relaxed).toEqual(['access:wheelchair', 'diet:vegetarian'])
    expect(twice.toggles.vegetarian).toBe(false)

    expect(toggleConstraint(twice, 'wheelchair').relaxed).toEqual(['diet:vegetarian']) // ticking it brings it back
    const restored = restoreConstraint(twice, 'diet:vegetarian')
    expect(restored.relaxed).toEqual(['access:wheelchair'])
    expect(restored.toggles.vegetarian).toBe(true)
  })

  it('applies relaxation patches to requests and the form', () => {
    const req = { free_text: 'x', budget: 300, neighbourhood: 'Indiranagar' }
    expect(applyPatch(req, { budget: 850, neighbourhood: null, not_a_field: 1 })).toEqual({ free_text: 'x', budget: 850 })
    const { form, dirty } = patchForm(defaultFormState(), {}, { budget: 850, constraints: ['vegetarian', 'no malls'] })
    expect(form.budget).toBe('850')
    expect(form.toggles.vegetarian).toBe(true)
    expect(form.toggles.avoid_crowds).toBe(false)
    expect(form.extraConstraints).toBe('no malls')
    expect(dirty).toEqual({ budget: true, constraints: true })
  })
})

describe('omission labels', () => {
  it('prefers the server name, then a free-text idea, then a neutral label', () => {
    expect(omissionLabel({ ref: 'gp:abc', name: 'Hoppy Yard Brewpub' })).toBe('Hoppy Yard Brewpub')
    expect(omissionLabel({ ref: 'street food market', name: null })).toBe('street food market')
    expect(omissionLabel({ ref: 'snap:xyz', name: null })).toBe('Another option')
    expect(omissionLabel({ ref: 'evfx:1' })).toBe('Another option')
    expect(omissionLabel({ ref: 'fx:1', name: '  ' })).toBe('Another option')
  })
})

describe('urls', () => {
  it('only accepts absolute https URLs', () => {
    expect(safeHttpsUrl('https://www.google.com/maps')).toBe('https://www.google.com/maps')
    expect(safeHttpsUrl('javascript:alert(1)')).toBeNull()
    expect(safeHttpsUrl(' JaVaScRiPt:alert(1)')).toBeNull()
    expect(safeHttpsUrl('data:text/html,<script>alert(1)</script>')).toBeNull()
    expect(safeHttpsUrl('http://example.com')).toBeNull()
    expect(safeHttpsUrl('//example.com/x')).toBeNull()
    expect(safeHttpsUrl('/relative')).toBeNull()
    expect(safeHttpsUrl('https://user:pw@example.com')).toBeNull()
    expect(safeHttpsUrl(42)).toBeNull()
  })

  it('builds a keyless Google Maps link when the provider URL is missing or unsafe', () => {
    const cafe = { ...plan.stops[0].candidate, maps_url: 'javascript:alert(1)' }
    const url = mapsUrlFor(cafe, 'Bangalore')
    expect(url.startsWith('https://www.google.com/maps/search/?api=1&query=')).toBe(true)
    expect(url).toContain('query_place_id=ChIJtest_courtyard_cafe')
  })
})

describe('share text', () => {
  it('renders the plan as plain text', () => {
    const text = planToText(plan, { shareUrl: 'https://saturday.example/p/sh_test123' })
    expect(text).toContain('My Saturday in Bangalore')
    expect(text).toContain('Saturday, 7 November 2026, 3:00 PM – 6:35 PM')
    expect(text).toContain('1. 3:00 PM – 4:00 PM  Courtyard Filter Coffee House (Café)')
    expect(text).toContain('₹250–₹450 per person, listed price')
    expect(text).toContain('then auto-rickshaw about 16 min, ₹60–₹90 estimate')
    expect(text).toContain('Plan B if it rains: Venkatappa Art Gallery')
    expect(text).toContain('Worst case per person: ₹1,143 of ₹2,000 budget')
    expect(text).toContain('Plan: https://saturday.example/p/sh_test123')
    expect(whatsappUrl('a b')).toBe('https://wa.me/?text=a%20b')
  })

  it('matches legs to stops by ID', () => {
    expect(legAfter(plan, 0)?.mode).toBe('walk')
    expect(legAfter(plan, 1)?.mode).toBe('auto')
    expect(legAfter(plan, 2)).toBeNull()
  })

  it('never pairs a leg by position: out-of-order and missing legs', () => {
    const reversed: CanonicalPlan = { ...plan, legs: [...plan.legs].reverse() }
    expect(legAfter(reversed, 0)?.mode).toBe('walk')
    expect(legAfter(reversed, 1)?.mode).toBe('auto')
    // Only the park -> gig leg is present: there is no leg after the café, even though legs[0] exists.
    const missing: CanonicalPlan = { ...plan, legs: [plan.legs[1]] }
    expect(legAfter(missing, 0)).toBeNull()
    expect(legAfter(missing, 1)?.mode).toBe('auto')
  })

  it('copies the same leg pairing as the timeline (by from_id/to_id)', () => {
    const lines = (p: CanonicalPlan) => planToText(p).split('\n')
    const legLineAfter = (text: string[], stopPrefix: string) => {
      const at = text.findIndex((l) => l.startsWith(stopPrefix))
      const next = text.slice(at + 1).find((l) => /^\d+\. /.test(l) || l.startsWith('   then '))
      return next?.startsWith('   then ') ? next.trim() : null
    }
    const reversed = lines({ ...plan, legs: [...plan.legs].reverse() })
    expect(legLineAfter(reversed, '1. ')).toBe('then walk about 12 min')
    expect(legLineAfter(reversed, '2. ')).toBe('then auto-rickshaw about 16 min, ₹60–₹90 estimate')
    expect(legLineAfter(reversed, '3. ')).toBeNull()

    const missing = lines({ ...plan, legs: [plan.legs[1]] })
    expect(legLineAfter(missing, '1. ')).toBeNull()
    expect(legLineAfter(missing, '2. ')).toBe('then auto-rickshaw about 16 min, ₹60–₹90 estimate')
    expect(lines(plan)).toEqual(lines({ ...plan, legs: [...plan.legs].reverse() }))
  })
})
