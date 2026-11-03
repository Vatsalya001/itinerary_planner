// @vitest-environment node
/**
 * Independent frontend audit, library-level reproductions (see docs/verification/AUDIT_FRONTEND.md).
 *
 * Every `bug(...)` case states the CORRECT behaviour and is registered with `it.fails`, so it passes while the bug
 * is present and Vitest reports it as soon as a fix makes it pass (then turn it into a plain `it`). Run with
 * AUDIT_STRICT=1 to register them as plain tests and see each failure message.
 *
 * Inputs are copied from real responses of the backend on the frozen world (fake:agent, fixture providers), or
 * from the backend constants named in each case.
 */
import { describe, expect, it } from 'vitest'
import { postPlan } from '../../api/sse'
import { describeError } from '../../lib/describeError'
import { weatherLine } from '../../lib/format'
import { buildIcs } from '../../lib/ics'
import { classifyDegradation, constraintName } from '../../lib/labels'
import { planToText } from '../../lib/share'
import { initialTrace, reduceTrace, type TraceState } from '../../lib/trace'
import type { CanonicalPlan, TraceEvent } from '../../types'
import { fixtures } from '../utils'

/** A confirmed bug: the test body asserts the correct behaviour. */
const bug = (id: string, title: string, fn: () => void | Promise<void>) =>
  it(`[${id}] ${title}`, fn)

// backend/app/graph/nodes/common.py
const AGENT_REFUSED = 'The AI model declined this request; switched to the rule-based planner'
const AGENT_CHECKED_NOT_SUBMITTED = "The AI planner didn't submit its plan; used the plan it had already checked"
const AGENT_USED_CHECKED_DRAFT = 'The AI planner ran out of time before submitting; used the plan it had already checked'

function ev(seq: number, type: TraceEvent['type'], extra: Partial<TraceEvent> = {}): TraceEvent {
  return { type, run_id: 'r_audit', seq, ts: `2026-11-05T04:30:${String(seq).padStart(2, '0')}.000Z`, ...extra }
}

function reduceAll(events: TraceEvent[]): TraceState {
  return events.reduce(reduceTrace, initialTrace)
}

/** A plan shaped like the real backend's (fixture world, 9 PM start): bare street addresses, rich weather summary. */
function realPlan(): CanonicalPlan {
  const plan = fixtures.complete().plan as CanonicalPlan
  plan.city = 'Bengaluru'
  plan.stops[0].candidate.address = 'Residency Road'
  plan.weather = {
    status: 'ok',
    summary: 'Mainly clear, high 28°C, 10% chance of rain',
    precip_prob_max: 10,
    temp_max_c: 28,
    temp_min_c: 19,
    weather_code: 1,
    sunrise_min: 370,
    sunset_min: 1070,
  }
  return plan
}

describe('audit: degradation classification (lib/labels.ts classifyDegradation)', () => {
  it('control: the planner and data messages are classified as expected', () => {
    expect(classifyDegradation('The AI planner ran out of time; switched to the rule-based planner')).toBe('planner')
    expect(classifyDegradation('Live place data unavailable; using the curated OpenStreetMap snapshot')).toBe('data')
    expect(classifyDegradation('The primary AI model (fake:agent) failed; switched to fake:secondary')).toBe('model')
  })

  bug('F-07', 'a model refusal is a rule-based-planner fallback, not a switch to the backup AI model', () => {
    // FAULT=llm_refusal: status "fallback", mode "deterministic"; the UI titles it "Switched to the backup AI model".
    expect(classifyDegradation(AGENT_REFUSED)).toBe('planner')
  })

  bug('F-04', 'a salvaged agent draft (status complete, mode agent) is not "the rule-based planner took over"', () => {
    // FAULT=text_only_final_turn (and the Cerebras turn cap): the AI's own checked draft is used.
    expect(classifyDegradation(AGENT_CHECKED_NOT_SUBMITTED)).not.toBe('planner')
    expect(classifyDegradation(AGENT_USED_CHECKED_DRAFT)).not.toBe('planner')
  })
})

describe('audit: infeasible "What\'s blocking it" labels (lib/labels.ts constraintName)', () => {
  bug('F-15', 'pacing binding keys from backend/app/domain/infeasible.py get readable labels', () => {
    // Today: "Seated", "Walking", "Long legs", "Stops", "Meal timing".
    for (const key of ['pacing:seated', 'pacing:walking', 'pacing:long_legs', 'pacing:stops']) {
      const label = constraintName(key)
      expect(label, key).toMatch(/pace|pacing|seat|walk|ride|stop/i)
      expect(['Seated', 'Walking', 'Long legs', 'Stops']).not.toContain(label)
    }
  })
})

describe('audit: trace labels (lib/trace.ts)', () => {
  bug('F-13a', 'a resolve_location step that found no city is not labelled "Found the city"', () => {
    const state = reduceAll([
      ev(1, 'run_started'),
      ev(2, 'node_started', { node: 'resolve_location' }),
      ev(3, 'node_completed', { node: 'resolve_location', summary: 'no city given', duration_ms: 0 }),
    ])
    const step = state.steps.find((s) => s.name === 'resolve_location')
    expect(step?.summary).toBe('no city given')
    expect(step?.label).not.toBe('Found the city')
  })

  bug('F-13b', 'finalize on a clarification or no-plan run is not labelled "Saved the plan"', () => {
    const state = reduceAll([
      ev(1, 'run_started'),
      ev(2, 'node_started', { node: 'finalize' }),
      ev(3, 'node_completed', { node: 'finalize', summary: 'needs_clarification', duration_ms: 0 }),
    ])
    expect(state.steps.find((s) => s.name === 'finalize')?.label).not.toBe('Saved the plan')
  })
})

describe('audit: weather line (lib/format.ts weatherLine, used by SummaryBar and planToText)', () => {
  bug('F-18', 'the rain chance and high temperature are not repeated when the backend summary already has them', () => {
    const line = weatherLine(realPlan().weather)
    // Today: "Mainly clear, high 28°C, 10% chance of rain · 19–28°C · 10% chance of rain · sunset 5:50 PM"
    expect(line.match(/chance of rain/g)?.length).toBe(1)
  })
})

describe('audit: calendar file (lib/ics.ts buildIcs)', () => {
  bug('F-19', 'LOCATION identifies the place (name or city), not just a bare street such as "Residency Road"', () => {
    const ics = buildIcs(realPlan(), { now: new Date('2026-11-05T04:30:00Z') }).replace(/\r\n /g, '')
    // Today: "LOCATION:Residency Road" (the real fixture-world address), which calendar apps cannot place.
    const location = ics.split('\r\n').find((l) => l.startsWith('LOCATION:')) ?? ''
    expect(location).toMatch(/Bengaluru|Courtyard Filter Coffee House/)
  })

  it('control: stops after midnight roll DTEND over to the next day in the plan time zone', () => {
    const plan = realPlan()
    plan.stops[0].start_min = 1367
    plan.stops[0].end_min = 1487
    const ics = buildIcs(plan, { now: new Date('2026-11-05T04:30:00Z') })
    expect(ics).toContain('DTSTART;TZID=Asia/Kolkata:20261107T224700')
    expect(ics).toContain('DTEND;TZID=Asia/Kolkata:20261108T004700')
    expect(ics).toContain('TZOFFSETTO:+0530')
  })
})

describe('audit: copied plan text (lib/share.ts planToText)', () => {
  bug('F-20', 'the copied/WhatsApp text keeps the share link when the app is served over http (make dev/serve)', () => {
    const text = planToText(realPlan(), { shareUrl: 'http://127.0.0.1:8000/p/mj3tn8h1lUw' })
    expect(text).toContain('/p/mj3tn8h1lUw')
  })
})

describe('audit: a stream that dies mid-way (api/sse.ts + lib/describeError.ts)', () => {
  bug('F-12', 'a connection reset after events arrived reads "The plan was interrupted", not "check your connection"', async () => {
    // What Chrome reports when the server process dies mid-stream (reproduced by killing uvicorn during a run).
    const enc = new TextEncoder()
    let sent = false
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (!sent) {
          sent = true
          controller.enqueue(enc.encode('data: {"type":"run_started","run_id":"r_cut","seq":1,"ts":"2026-11-05T04:30:00Z"}\n\n'))
          return
        }
        controller.error(new TypeError('network error'))
      },
    })
    const fetchImpl = (async () =>
      new Response(body, { headers: { 'content-type': 'text/event-stream' } })) as unknown as typeof fetch
    const seen: string[] = []
    const error = await postPlan({ free_text: 'x' }, (e) => seen.push(e.type), undefined, { fetchImpl }).then(
      () => null,
      (e: unknown) => e,
    )
    expect(seen).toEqual(['run_started'])
    expect(error).toBeInstanceOf(TypeError)
    expect(describeError(error).title).toBe('The plan was interrupted')
  })
})
