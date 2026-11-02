import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import evals from '../test/fixtures/evals_summary.json'
import { fixtures } from '../test/utils'
import EvalsLeaderboard from './EvalsLeaderboard'
import HowItWorks from './HowItWorks'
import SharedPlan from './SharedPlan'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SharedPlan', () => {
  function renderAt(path: string) {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/p/:id" element={<SharedPlan />} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('loads and renders a shared plan, then focuses its heading', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(fixtures.complete()))
    vi.stubGlobal('fetch', fetchMock)
    renderAt('/p/sh_test123')
    const heading = await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })
    await waitFor(() => expect(heading).toHaveFocus())
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe('/api/plans/sh_test123')
    expect(screen.getAllByRole('article')).toHaveLength(3)
  })

  it('explains an unknown or expired link and focuses the explanation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'Plan not found' }, 404)))
    renderAt('/p/nope')
    const heading = await screen.findByRole('heading', { name: "This plan isn't available" })
    await waitFor(() => expect(heading).toHaveFocus())
  })

  it('shows a retryable error (not an endless "Loading") when the request is aborted by something else', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new DOMException('The user aborted a request.', 'AbortError'))
      .mockResolvedValueOnce(jsonResponse(fixtures.complete()))
    vi.stubGlobal('fetch', fetchMock)
    renderAt('/p/sh_test123')
    expect(await screen.findByRole('heading', { name: 'The plan was interrupted' })).toBeInTheDocument()
    expect(screen.queryByText('Loading the plan…')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('EvalsLeaderboard', () => {
  it('groups rows by kind and renders missing fields defensively', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(evals)))
    render(
      <MemoryRouter>
        <EvalsLeaderboard />
      </MemoryRouter>,
    )
    const api = await screen.findByRole('region', { name: 'Hosted API models' })
    const claude = within(api).getByRole('row', { name: /anthropic:claude-opus-5-5/ })
    expect(within(claude).getByText('95%')).toBeInTheDocument()
    expect(within(claude).getByText('$0.19')).toBeInTheDocument()
    expect(within(claude).getByText('17 s')).toBeInTheDocument()

    const local = screen.getByRole('region', { name: 'Local models (Ollama)' })
    const gemma = within(local).getByRole('row', { name: /ollama:gemma4:e4b/ })
    expect(within(gemma).getAllByText('—').length).toBeGreaterThan(5)
    expect(screen.getByRole('region', { name: 'Baseline (no LLM)' })).toBeInTheDocument()
    expect(screen.getByText('Commit abc1234def56')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'By category' })).toBeInTheDocument()
  })

  it('marks rows measured with earlier prompts', async () => {
    const summary = {
      generated_at: '2026-11-03T00:00:00Z',
      rows: [
        { model: 'cerebras:gpt-oss-120b', kind: 'api', runs: 26, hard_pass_rate: 0.9, prompt_current: true },
        { model: 'ollama:granite4:tiny-h', kind: 'local', runs: 26, hard_pass_rate: 0.42, prompt_current: false },
      ],
    }
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(summary)))
    render(
      <MemoryRouter>
        <EvalsLeaderboard />
      </MemoryRouter>,
    )
    const local = await screen.findByRole('region', { name: 'Local models (Ollama)' })
    expect(within(local).getByText('earlier prompts')).toBeInTheDocument()
    const api = screen.getByRole('region', { name: 'Hosted API models' })
    expect(within(api).queryByText('earlier prompts')).not.toBeInTheDocument()
  })

  it('reports an abort it did not cause instead of loading forever', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new DOMException('signal timed out', 'TimeoutError'))))
    render(
      <MemoryRouter>
        <EvalsLeaderboard />
      </MemoryRouter>,
    )
    expect(await screen.findByText('The planner took too long to answer. Please try again.')).toBeInTheDocument()
    expect(screen.queryByText('Loading results…')).not.toBeInTheDocument()
  })

  it('handles an empty or unavailable report', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'not found' }, 404)))
    render(
      <MemoryRouter>
        <EvalsLeaderboard />
      </MemoryRouter>,
    )
    expect(await screen.findByRole('heading', { name: 'No results published yet' })).toBeInTheDocument()
  })
})

describe('HowItWorks', () => {
  it('lists the graph nodes and tools', () => {
    render(
      <MemoryRouter>
        <HowItWorks />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { level: 1, name: 'How it works' })).toBeInTheDocument()
    expect(screen.getByText('validate_plan')).toBeInTheDocument()
    expect(screen.getByText('search_places')).toBeInTheDocument()
  })

  it('describes the graph as the code implements it (docs/LLD.md section 7.3)', () => {
    render(
      <MemoryRouter>
        <HowItWorks />
      </MemoryRouter>,
    )
    const graph = screen.getByRole('region', { name: 'The graph' })
    const nodes = Array.from(graph.querySelectorAll('ol code')).map((c) => c.textContent)
    // graph/build.py order; no nodes that do not exist (get_weather/compute_window were plan-only)
    expect(nodes).toEqual([
      'guard_input',
      'parse_preferences',
      'resolve_location',
      'check_clarity',
      'ask_clarification',
      'build_context',
      'plan_agent',
      'tools',
      'validate_plan',
      'fallback_planner',
      'explain_infeasible',
      'finalize',
    ])
    // guard_input normalises and flags; rejection happens at the API (400/413/429)
    expect(screen.getByText(/It never rejects a request/)).toHaveTextContent(/HTTP 400, 413 or 429/)
    expect(screen.queryByText(/rejecting anything/)).not.toBeInTheDocument()
    // parsing is rules + LLM, grounded in the user's words
    expect(screen.getByText('rules + LLM')).toBeInTheDocument()
    expect(screen.getByText(/grounded in your words/)).toBeInTheDocument()
    // finalize builds and emits; the API persists and the runner deletes the checkpoint
    expect(screen.getByText(/emits the run metrics and the result/)).toHaveTextContent(
      /the API saves shareable plans and the run log, and the runner deletes the finished run’s checkpoint/,
    )
  })
})
