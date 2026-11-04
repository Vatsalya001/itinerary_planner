import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { postPlan } from '../api/sse'
import { HISTORY_KEY, readHistory } from '../lib/itineraryHistory'
import { fixtures } from '../test/utils'
import Planner from './Planner'

vi.mock('../api/sse', async (original) => ({ ...await original<typeof import('../api/sse')>(), postPlan: vi.fn() }))
const post = vi.mocked(postPlan)
const mount = () => render(<MemoryRouter><Planner /></MemoryRouter>)
const history = () => screen.getByRole('region', { name: 'Your itinerary history' })

beforeEach(() => { localStorage.clear(); post.mockReset() })
afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

async function plan(id = 'sh_test123', city = 'Gurugram') {
  const result = fixtures.complete()
  result.run_id = `run_${id}`; result.share_id = id; result.plan!.city = city
  post.mockResolvedValueOnce(result)
  fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
  await screen.findByRole('heading', { name: `Your Saturday in ${city}` })
  await waitFor(() => expect(within(history()).getByRole('link', { name: `Open itinerary for ${city} on 2026-11-07` })).toBeInTheDocument())
}

it('saves a city-labelled plan once and restores history after remount without another model call', async () => {
  const app = mount()
  await plan()
  expect(readHistory(localStorage).entries).toHaveLength(1)
  const link = within(history()).getByRole('link', { name: /Open itinerary for Gurugram/ })
  expect(link).toHaveAttribute('href', '/p/sh_test123')
  app.unmount(); mount()
  expect(within(history()).getByRole('link', { name: /Open itinerary for Gurugram/ })).toBeInTheDocument()
  expect(post).toHaveBeenCalledTimes(1)
})

it('removes the displayed plan without re-saving it or deleting other local storage', async () => {
  localStorage.setItem('unrelated', 'keep')
  mount(); await plan()
  fireEvent.click(within(history()).getByRole('button', { name: /Remove itinerary for Gurugram/ }))
  expect(within(history()).queryByRole('link')).not.toBeInTheDocument()
  expect(readHistory(localStorage).entries).toHaveLength(0)
  expect(localStorage.getItem('unrelated')).toBe('keep')
  expect(screen.getByRole('heading', { name: 'Your Saturday in Gurugram' })).toBeInTheDocument()
})

it('confirms clearing history and supports cancellation', async () => {
  mount(); await plan()
  fireEvent.click(within(history()).getByRole('button', { name: 'Clear history' }))
  fireEvent.click(within(history()).getByRole('button', { name: 'Keep history' }))
  expect(readHistory(localStorage).entries).toHaveLength(1)
  fireEvent.click(within(history()).getByRole('button', { name: 'Clear history' }))
  fireEvent.click(within(history()).getByRole('button', { name: 'Yes, clear history' }))
  expect(readHistory(localStorage).entries).toHaveLength(0)
})

it('keeps planning usable with corrupt storage', async () => {
  localStorage.setItem(HISTORY_KEY, '{invalid')
  mount()
  expect(within(history()).getByRole('status')).toHaveTextContent('could not be read')
  await plan()
  expect(readHistory(localStorage).entries).toHaveLength(1)
})

it('retains in-memory history when browser storage is full', async () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  mount(); await plan('first'); await plan('second', 'Mysuru')
  expect(within(history()).getAllByRole('link')).toHaveLength(2)
  expect(within(history()).getByRole('status')).toHaveTextContent('only in this tab')
})

it('synchronizes another tab adding or clearing bookmarks', async () => {
  mount()
  localStorage.setItem(HISTORY_KEY, JSON.stringify({ version: 1, entries: [{
    shareId: 'other_tab', city: 'Bundi', date: '2026-11-07', prompt: 'An art outing', savedAt: Date.now(),
  }] }))
  fireEvent(window, new StorageEvent('storage', { key: HISTORY_KEY }))
  expect(within(history()).getByRole('link', { name: /Open itinerary for Bundi/ })).toBeInTheDocument()
  localStorage.removeItem(HISTORY_KEY)
  fireEvent(window, new StorageEvent('storage', { key: null }))
  expect(within(history()).queryByRole('link')).not.toBeInTheDocument()
})

it('does not save clarification or infeasible responses', async () => {
  mount()
  post.mockResolvedValueOnce(fixtures.clarification())
  fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
  await screen.findByRole('heading', { name: /quick question/i })
  expect(readHistory(localStorage).entries).toHaveLength(0)
})


it('explains missing share links while keeping the completed plan usable', async () => {
  const result = fixtures.complete(); result.share_id = null
  post.mockResolvedValueOnce(result)
  mount()
  fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
  await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })
  expect(await within(history()).findByRole('status')).toHaveTextContent('saved-plan link is unavailable')
  expect(readHistory(localStorage).entries).toHaveLength(0)
})
