// @vitest-environment node
import { describe, expect, it } from 'vitest'
import complete from '../test/fixtures/plan_complete.json'
import traceEvents from '../test/fixtures/trace_events.json'
import type { CanonicalPlan, PlanResult, TraceEvent } from '../types'
import { DEFAULT_FREE_TEXT, buildRequest, defaultFormState, patchForm, validateForm } from './form'
import { applyPatch, legAfter } from './plan'
import { planToText, whatsappUrl } from './share'
import { initialTrace, reduceTrace } from './trace'
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
    expect(state.degraded).toEqual(["Couldn't check events"])
    const validations = state.steps.filter((s) => s.kind === 'validation')
    expect(validations.map((v) => v.status)).toEqual(['warn', 'done'])
    expect(validations[0].details).toEqual(['Worst case is above the budget'])
    expect(state.steps.at(-1)).toMatchObject({ kind: 'result', label: 'Plan ready' })
    expect(state.finished).toBe(true)
  })

  it('ignores duplicate deliveries and heartbeats', () => {
    const again = reduceTrace(state, events[3])
    expect(again).toBe(state)
    const hb = reduceTrace(state, { type: 'heartbeat', run_id: 'r', seq: 0, ts: '2026-11-05T04:31:00Z' })
    expect(hb.steps).toBe(state.steps)
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
})
