import { render, screen, within } from '@testing-library/react'
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

  it('loads and renders a shared plan', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(fixtures.complete()))
    vi.stubGlobal('fetch', fetchMock)
    renderAt('/p/sh_test123')
    expect(await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })).toBeInTheDocument()
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe('/api/plans/sh_test123')
    expect(screen.getAllByRole('article')).toHaveLength(3)
  })

  it('explains an unknown or expired link', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'Plan not found' }, 404)))
    renderAt('/p/nope')
    expect(await screen.findByRole('heading', { name: "This plan isn't available" })).toBeInTheDocument()
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
})
