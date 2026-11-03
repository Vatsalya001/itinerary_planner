/**
 * Independent frontend audit, component and page reproductions (see docs/verification/AUDIT_FRONTEND.md).
 *
 * Every `bug(...)` case states the CORRECT behaviour and is registered with `it.fails`, so it passes while the bug
 * is present and Vitest reports it as soon as a fix makes it pass (then turn it into a plain `it`). Run with
 * AUDIT_STRICT=1 to register them as plain tests and see each failure message.
 *
 * Result payloads copy the shapes and messages the real backend returned on the frozen world (fake:agent, fixture
 * providers, FAULT=... injections); see the audit report for the exact runs.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../../api/errors'
import { postPlan, resumePlan } from '../../api/sse'
import { ErrorCard } from '../../components/ErrorCard'
import EvalsLeaderboard from '../../pages/EvalsLeaderboard'
import Planner from '../../pages/Planner'
import type { PlanResult, TraceEvent } from '../../types'
import realSummary from '../fixtures/evals_summary.json'
import { fixtures } from '../utils'

vi.mock('../../api/sse', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/sse')>()
  return { ...actual, postPlan: vi.fn(), resumePlan: vi.fn() }
})

const mockedPost = vi.mocked(postPlan)
const mockedResume = vi.mocked(resumePlan)

/** A confirmed bug: the test body asserts the correct behaviour. */
const bug = (id: string, title: string, fn: () => void | Promise<void>) =>
  it(`[${id}] ${title}`, fn)

// Messages emitted by the backend (backend/app/graph/nodes/common.py, app/pipeline.py, app/graph/nodes/clarify.py).
const AGENT_REFUSED = 'The AI model declined this request; switched to the rule-based planner'
const AGENT_CHECKED_NOT_SUBMITTED = "The AI planner didn't submit its plan; used the plan it had already checked"
const SNAPSHOT_USED = 'Live place data unavailable; using the curated OpenStreetMap snapshot'
const LOOKUP_FAILED = "I couldn't look up that city right now. Please try again shortly."

function renderPlanner() {
  return render(
    <MemoryRouter>
      <Planner />
    </MemoryRouter>,
  )
}

function ev(seq: number, type: TraceEvent['type'], extra: Partial<TraceEvent> = {}): TraceEvent {
  return { type, run_id: 'r_audit', seq, ts: `2026-11-05T04:30:${String(seq).padStart(2, '0')}.000Z`, ...extra }
}

/** True when the element is not inside a `hidden` subtree (what a sighted user can see). */
function shown(el: Element): boolean {
  return el.closest('[hidden]') === null
}

const plan = () => fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
const openDetails = () => fireEvent.click(screen.getByRole('button', { name: 'Fine-tune the details' }))
const hideDetails = () => fireEvent.click(screen.getByRole('button', { name: 'Hide the details' }))
const describeAs = (text: string) =>
  fireEvent.change(screen.getByLabelText('Describe your Saturday'), { target: { value: text } })

beforeEach(() => {
  mockedPost.mockReset()
  mockedResume.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('audit: planner form semantics', () => {
  bug('F-01', 'a chip edited earlier does not silently override the interests of a rewritten description', async () => {
    mockedPost.mockResolvedValueOnce(fixtures.complete())
    renderPlanner()
    openDetails()
    fireEvent.click(screen.getByRole('button', { name: 'Coffee' }))
    hideDetails()
    describeAs("I'm in Bangalore with ₹2000 and 4 hours this Saturday. Chill mood. I love art galleries and museums.")
    // Anything outside the collapsed panel that tells the user the chips will override the description.
    const visibleHint = [...document.querySelectorAll('main, form *')].some(
      (el) => el.children.length === 0 && /edited|override/i.test(el.textContent ?? '') && shown(el),
    )
    plan()
    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1))
    const sent = mockedPost.mock.calls[0][0]
    // Today: {free_text: "...art galleries and museums.", interests: ["food","music","walks","coffee"]}, and the
    // backend (parser_rules.merge_parse) then ignores the interests written in the description.
    expect(sent.interests === undefined || visibleHint).toBe(true)
  })

  bug('F-02', 'unticking a must-have that the description states either drops it or the UI stops promising an override', async () => {
    mockedPost.mockResolvedValueOnce(fixtures.complete())
    renderPlanner() // default description says "I'm vegetarian"
    openDetails()
    const note = screen.getByText(/Fields you change here/).textContent ?? ''
    fireEvent.click(screen.getByLabelText('Vegetarian'))
    expect(screen.getByLabelText('Vegetarian')).not.toBeChecked()
    plan()
    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1))
    const sent = mockedPost.mock.calls[0][0]
    // Today the request is just {free_text}; the backend unions text constraints, so vegetarian stays enforced
    // while the panel says "Fields you change here override what the planner reads from your description."
    expect(Boolean(sent.relaxed_constraints?.includes('diet:vegetarian')) || !/override/i.test(note)).toBe(true)
  })
})

describe('audit: relaxations and clarifications', () => {
  bug('F-03', 'a relaxation after a clarification keeps the answers instead of asking the same questions again', async () => {
    const clarification: PlanResult = {
      ...fixtures.clarification(),
      thread_id: 'th_audit',
      questions: [
        { id: 'city', question: 'Which city are you spending Saturday in?', options: ['Bangalore', 'Mumbai'], field: 'city' },
        {
          id: 'budget_time',
          question: "Roughly how much time do you have, and what's your budget per person?",
          options: ['3 hours, ₹1000', '4 hours, ₹2000'],
          field: null,
        },
      ],
    }
    const infeasible = fixtures.infeasible()
    infeasible.preferences = { ...infeasible.preferences!, city: 'Bangalore', budget: 2000, available_minutes: 240 }
    infeasible.infeasible!.relaxations = [
      { label: "Drop 'need wheelchair access'", patch: { relaxed_constraints: ['access:wheelchair'] } },
    ]
    mockedPost.mockResolvedValueOnce(clarification).mockResolvedValueOnce(fixtures.complete())
    mockedResume.mockResolvedValueOnce(infeasible)

    renderPlanner()
    describeAs("I'm tired. I'm vegan and jain, need wheelchair access and want to avoid crowded places.")
    plan()
    fireEvent.click(await screen.findByRole('button', { name: 'Bangalore' }))
    fireEvent.click(screen.getByRole('button', { name: '4 hours, ₹2000' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue planning' }))
    fireEvent.click(await screen.findByRole('button', { name: "Drop 'need wheelchair access'" }))
    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(2))
    // Today: {free_text: "I'm tired. ...", relaxed_constraints: ["access:wheelchair"]}: no city, budget or time,
    // so the real backend answers needs_clarification with the same two questions.
    const sent = JSON.stringify(mockedPost.mock.calls[1][0])
    expect(sent).toMatch(/Bangalore/)
    expect(sent).toMatch(/2000|4 hours|240/)
  })

  bug('F-05', 'a no_feasible_plan without a report (lookup outage, unknown city) is not "No plan fits everything"', async () => {
    // FAULT=geocoder_down, places outage outside the snapshot, or an unknown city typed into the clarification.
    mockedPost.mockResolvedValueOnce({ ...fixtures.infeasible(), infeasible: null, message: LOOKUP_FAILED })
    renderPlanner()
    plan()
    expect(await screen.findByText(LOOKUP_FAILED)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'No plan fits everything' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Try changing the budget, time or constraints/)).not.toBeInTheDocument()
  })

  bug('F-17', '"What I understood" does not present defaults (4 h) as understood while asking for the time', async () => {
    const clarification: PlanResult = {
      ...fixtures.clarification(),
      questions: [
        {
          id: 'budget_time',
          question: "Roughly how much time do you have, and what's your budget per person?",
          options: ['3 hours, ₹1000'],
          field: null,
        },
      ],
    }
    clarification.preferences = { ...clarification.preferences!, budget: null, available_minutes: 240 }
    mockedPost.mockResolvedValueOnce(clarification)
    renderPlanner()
    describeAs('plan something fun for saturday')
    plan()
    await screen.findByRole('heading', { name: 'One quick question' })
    const understood = screen.getByRole('region', { name: 'What I understood' })
    expect(within(understood).queryByText('4 h')).not.toBeInTheDocument()
  })
})

describe('audit: degraded results', () => {
  bug('F-04', 'a salvaged agent draft (complete, agent mode) is not announced as "The rule-based planner took over"', async () => {
    mockedPost.mockResolvedValueOnce({
      ...fixtures.complete(),
      mode: 'agent',
      model: 'cerebras:gpt-oss-120b',
      degradations: [AGENT_CHECKED_NOT_SUBMITTED],
    })
    renderPlanner()
    plan()
    await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })
    expect(screen.getByText('Planned with cerebras:gpt-oss-120b and checked by the validator.')).toBeInTheDocument()
    expect(screen.queryByText('The rule-based planner took over')).not.toBeInTheDocument()
    expect(screen.queryByText(/a simpler rule-based planner built the plan/)).not.toBeInTheDocument()
  })

  bug('F-07', 'a refusal fallback is not titled "Switched to the backup AI model"', async () => {
    mockedPost.mockResolvedValueOnce({ ...fixtures.fallback(), degradations: [AGENT_REFUSED] })
    renderPlanner()
    plan()
    await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })
    expect(screen.getByText('This is a backup plan')).toBeInTheDocument()
    expect(screen.queryByText('Switched to the backup AI model')).not.toBeInTheDocument()
  })

  bug('F-09', 'a degradation notice is shown once, not in both the trace panel and the plan', async () => {
    mockedPost.mockImplementationOnce(async (_req, onEvent) => {
      onEvent(ev(1, 'run_started'))
      onEvent(ev(2, 'degraded', { node: 'tools', summary: SNAPSHOT_USED, data: { tool: 'search_places' } }))
      return { ...fixtures.complete(), degradations: [SNAPSHOT_USED] }
    })
    renderPlanner()
    plan()
    await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })
    expect(screen.getAllByText(SNAPSHOT_USED).filter(shown)).toHaveLength(1)
  })

  bug('F-10', 'a server-side failure inside the stream offers "Try again" and is not titled "I can\'t plan that"', async () => {
    // backend/app/api/routes.py produce_events / app/graph/runner.py: an "error" event, then a "rejected" result.
    mockedPost.mockImplementationOnce(async (_req, onEvent) => {
      onEvent(ev(1, 'run_started'))
      onEvent(ev(2, 'error', { summary: 'Something went wrong' }))
      return { ...fixtures.rejected(), message: 'Something went wrong. Please try again (reference r_audit).' }
    })
    renderPlanner()
    plan()
    expect(await screen.findByText(/Please try again \(reference r_audit\)/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: "I can't plan that" })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  bug('F-24', 'a 413/400 rejection does not offer a "Try again" that resends the same rejected request', async () => {
    mockedPost.mockRejectedValue(new ApiError(413, 'Request too large'))
    renderPlanner()
    plan()
    await screen.findByRole('heading', { name: "I can't plan that" })
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })
})

describe('audit: stopping a run', () => {
  function hangUntilAborted(events: TraceEvent[] = []) {
    mockedPost.mockImplementationOnce(
      (_req, onEvent, signal) =>
        new Promise((_res, rej) => {
          for (const e of events) onEvent(e)
          signal?.addEventListener('abort', () => rej(new DOMException('The operation was aborted.', 'AbortError')))
        }),
    )
  }

  bug('F-11a', 'pressing Stop keeps keyboard focus in the form instead of dropping it to <body>', async () => {
    hangUntilAborted()
    renderPlanner()
    plan()
    const stop = await screen.findByRole('button', { name: 'Stop' })
    stop.focus()
    fireEvent.click(stop)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Plan my Saturday' })).toBeEnabled())
    expect(document.activeElement).not.toBe(document.body)
  })

  bug('F-11b', 'after Stop no trace step is left "In progress" with a spinner', async () => {
    hangUntilAborted([ev(1, 'run_started'), ev(2, 'node_started', { node: 'plan_agent' })])
    renderPlanner()
    plan()
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Show steps' }))
    expect(screen.getByText('Agent is choosing the next step')).toBeVisible()
    expect(screen.queryByText(/In progress/)).not.toBeInTheDocument()
  })
})

describe('audit: form validation feedback', () => {
  bug('F-14a', 'an invalid field inside the collapsed details panel is shown or announced when Plan is pressed', () => {
    renderPlanner()
    openDetails()
    fireEvent.change(screen.getByLabelText('Group size'), { target: { value: '25' } })
    hideDetails()
    plan()
    expect(mockedPost).not.toHaveBeenCalled()
    const visibleError = screen.queryAllByText('Group size is 1 to 20.').some(shown)
    expect(visibleError || screen.queryAllByRole('alert').length > 0).toBe(true)
  })

  bug('F-14b', 'the group-size error is linked to its input with aria-describedby', () => {
    renderPlanner()
    openDetails()
    fireEvent.change(screen.getByLabelText('Group size'), { target: { value: '25' } })
    plan()
    const error = screen.getByText('Group size is 1 to 20.')
    expect(error).toBeVisible()
    // The label reads "Group size" plus the "edited" badge once the field is changed.
    const input = screen.getByLabelText(/^Group size/)
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input.getAttribute('aria-describedby') ?? '').toContain(error.id)
  })

  bug('F-25', 'a custom interest stays available as an unpressed chip after it is deselected', () => {
    renderPlanner()
    openDetails()
    fireEvent.change(screen.getByLabelText('Add another interest'), { target: { value: 'pottery' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    fireEvent.click(screen.getByRole('button', { name: 'Pottery' }))
    // Today the chip disappears, so the interest can only be re-added by typing it again.
    const chip = screen.queryByRole('button', { name: 'Pottery' })
    expect(chip).not.toBeNull()
    expect(chip).toHaveAttribute('aria-pressed', 'false')
  })
})

describe('audit: rate-limit countdown', () => {
  bug('F-16', 'the polite live region does not change every second for a 735-second Retry-After', () => {
    vi.useFakeTimers()
    const { container } = render(
      <ErrorCard title="The planner is busy" message="Too many plans; try again in 735 seconds" retryAfter={735} onRetry={() => {}} />,
    )
    const texts = new Set<string>()
    for (let i = 0; i < 6; i += 1) {
      for (const el of container.querySelectorAll('[aria-live]')) texts.add(el.textContent ?? '')
      act(() => {
        vi.advanceTimersByTime(1000)
      })
    }
    // Today: "Try again in 735 seconds.", "... 734 ...", ... one polite announcement per second for 12 minutes.
    expect(texts.size).toBeLessThanOrEqual(2)
  })
})

describe('audit: /evals with the committed backend/evals/results/summary.json', () => {
  bug('F-22', 'a partial row (Groq: 5 of 26 cases) is marked as partial like docs/EVAL_REPORT.md does', async () => {
    const summary = { ...realSummary, rows: [{ ...realSummary.rows[0], model: 'groq:openai/gpt-oss-120b',
      runs: 5, coverage: { cases: 5, suite_cases: 26, partial: true } }] }
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(summary), { headers: { 'content-type': 'application/json' } })),
    )
    render(
      <MemoryRouter>
        <EvalsLeaderboard />
      </MemoryRouter>,
    )
    const header = await screen.findByRole('rowheader', { name: /groq:openai\/gpt-oss-120b/ })
    expect(header.closest('tr')?.textContent ?? '').toMatch(/partial|5\s*\/\s*26|5 of 26/i)
  })
})
