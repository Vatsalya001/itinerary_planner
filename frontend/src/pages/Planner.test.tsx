import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/errors'
import { postPlan, resumePlan } from '../api/sse'
import { DEFAULT_FREE_TEXT } from '../lib/form'
import traceEvents from '../test/fixtures/trace_events.json'
import { fixtures } from '../test/utils'
import type { TraceEvent } from '../types'
import Planner from './Planner'

vi.mock('../api/sse', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/sse')>()
  return { ...actual, postPlan: vi.fn(), resumePlan: vi.fn() }
})

const mockedPost = vi.mocked(postPlan)
const mockedResume = vi.mocked(resumePlan)

function renderPlanner() {
  return render(
    <MemoryRouter>
      <Planner />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockedPost.mockReset()
  mockedResume.mockReset()
})

describe('Planner', () => {
  it('prefills the brief and its structured form', () => {
    renderPlanner()
    expect(screen.getByLabelText('Describe your Saturday')).toHaveValue(DEFAULT_FREE_TEXT)
    fireEvent.click(screen.getByRole('button', { name: 'Fine-tune the details' }))
    expect(screen.getByLabelText('City')).toHaveValue('Bangalore')
    expect(screen.getByLabelText('Budget per person')).toHaveValue(2000)
    expect(screen.getByLabelText('Time you have')).toHaveValue('240')
    expect(screen.getByLabelText('Mood')).toHaveValue('tired but wants to do something fun')
    expect(screen.getByRole('button', { name: 'Food' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Music' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Walks' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Coffee' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByLabelText('Vegetarian')).toBeChecked()
    expect(screen.getByLabelText('Avoid crowded places')).toBeChecked()
    expect(screen.getByLabelText('Vegan')).not.toBeChecked()
  })

  it('streams the trace and renders the plan', async () => {
    mockedPost.mockImplementationOnce(async (_req, onEvent) => {
      for (const ev of traceEvents as unknown as TraceEvent[]) onEvent(ev)
      return fixtures.complete()
    })
    renderPlanner()
    fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))

    expect(await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })).toHaveFocus()
    expect(mockedPost).toHaveBeenCalledWith({ free_text: DEFAULT_FREE_TEXT }, expect.any(Function), expect.any(AbortSignal))

    // trace is collapsible after the run; its steps are real events
    const toggle = screen.getByRole('button', { name: /Show steps/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(screen.getByText('Searched for places')).toBeVisible()
    expect(screen.getByText('Need a quiet vegetarian café for a seated first stop')).toBeVisible()
    expect(screen.getByText('8 places, 5 with vegetarian evidence, 2 likely busy')).toBeVisible()
    expect(screen.getByText('Event search failed')).toBeVisible()
    expect(screen.getByText('Checks found problems to repair')).toBeVisible()
    expect(screen.getByText('Worst case is above the budget')).toBeVisible()
    expect(screen.getByText('Plan passed the checks')).toBeVisible()
    expect(screen.getAllByText("Couldn't check events").length).toBeGreaterThan(0)

    // "What I understood"
    const understood = screen.getByRole('region', { name: 'What I understood' })
    expect(within(understood).getByText('₹2,000 per person')).toBeInTheDocument()
    expect(within(understood).getByText('Low energy')).toBeInTheDocument()
    expect(within(understood).getByText('Avoid crowded places')).toBeInTheDocument()
  })

  it('answers a clarification by resuming the paused run with the chosen option', async () => {
    mockedPost.mockResolvedValueOnce(fixtures.clarification())
    mockedResume.mockResolvedValueOnce(fixtures.complete())
    renderPlanner()
    fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))

    expect(await screen.findByText('Which city are you in?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Bangalore' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue planning' }))

    await waitFor(() =>
      expect(mockedResume).toHaveBeenCalledWith(
        'th_test789',
        { city: 'Bangalore' },
        expect.any(Function),
        expect.any(AbortSignal),
      ),
    )
    expect(await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })).toBeInTheDocument()
  })

  it('resubmits the request with a relaxation patch applied', async () => {
    mockedPost.mockResolvedValueOnce(fixtures.infeasible()).mockResolvedValueOnce(fixtures.complete())
    renderPlanner()
    fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Raise budget to ₹850' }))
    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(2))
    expect(mockedPost.mock.calls[1][0]).toEqual({ free_text: DEFAULT_FREE_TEXT, budget: 850 })
    expect(await screen.findByRole('heading', { name: 'Your Saturday in Bangalore' })).toBeInTheDocument()

    // the form reflects the change
    fireEvent.click(screen.getByRole('button', { name: 'Fine-tune the details' }))
    expect(screen.getByLabelText(/Budget per person/)).toHaveValue(850)
  })

  it('shows a friendly rate-limit message with the retry time', async () => {
    mockedPost.mockRejectedValueOnce(new ApiError(429, 'Too many plans from this address.', 12))
    renderPlanner()
    fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
    expect(await screen.findByRole('heading', { name: 'The planner is busy' })).toBeInTheDocument()
    expect(screen.getByText('Too many plans from this address.')).toBeInTheDocument()
    expect(screen.getByText('Try again in 12 seconds.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeDisabled()
  })

  it('shows validation errors (HTTP 400) as a friendly card', async () => {
    mockedPost.mockRejectedValueOnce(new ApiError(400, 'budget: Input should be greater than or equal to 0'))
    renderPlanner()
    fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
    expect(await screen.findByRole('heading', { name: "I can't plan that" })).toHaveFocus()
    expect(screen.getByText('budget: Input should be greater than or equal to 0')).toBeInTheDocument()
  })

  it('shows rejected results as an error card', async () => {
    mockedPost.mockResolvedValueOnce(fixtures.rejected())
    renderPlanner()
    fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
    expect(await screen.findByRole('heading', { name: "I can't plan that" })).toBeInTheDocument()
    expect(screen.getByText("I can only plan leisure outings. I can't book or pay for anything.")).toBeInTheDocument()
  })

  it('sends only the structured fields when the description is cleared', async () => {
    mockedPost.mockResolvedValueOnce(fixtures.complete())
    renderPlanner()
    fireEvent.change(screen.getByLabelText('Describe your Saturday'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Plan my Saturday' }))
    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1))
    expect(mockedPost.mock.calls[0][0]).toEqual({
      city: 'Bangalore',
      budget: 2000,
      available_time: 240,
      mood: 'tired but wants to do something fun',
      interests: ['food', 'music', 'walks'],
      constraints: ['vegetarian', 'avoid crowded places'],
    })
  })
})
