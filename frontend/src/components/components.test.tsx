import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fixtures } from '../test/utils'
import type { CheckItem } from '../types'
import { ChecksPanel } from './ChecksPanel'
import { ClarificationCard } from './ClarificationCard'
import { InfeasibleCard } from './InfeasibleCard'
import { PlanActions } from './PlanActions'
import { PlanView } from './PlanView'

describe('PlanView', () => {
  it('renders the fixture plan: times, costs, badges, legs and attribution', () => {
    const result = fixtures.complete()
    render(<PlanView result={result} shareUrl="https://saturday.example/p/sh_test123" />)

    expect(screen.getByRole('heading', { level: 2, name: 'Your Saturday in Bangalore' })).toBeInTheDocument()
    expect(screen.getByText('Planned by anthropic:claude-opus-5-5 and checked by the validator.')).toBeInTheDocument()

    // summary bar
    expect(screen.getByText('3:00 PM – 6:35 PM')).toBeInTheDocument()
    expect(screen.getByText('Saturday, 7 November 2026 · 3 h 35 min')).toBeInTheDocument()
    expect(screen.getByText('Partly cloudy')).toBeInTheDocument()
    expect(screen.getByText('19–28°C · 20% chance of rain · sunset 5:45 PM')).toBeInTheDocument()
    expect(screen.getByText('₹1,143')).toBeInTheDocument()
    expect(screen.getByText('of ₹2,000')).toBeInTheDocument()
    expect(screen.getByText('₹857 spare')).toBeInTheDocument()
    expect(screen.getByRole('meter')).toHaveAttribute('aria-valuetext', '₹1,143 of ₹2,000')
    expect(screen.getByText('Within budget')).toBeInTheDocument()

    // timeline cards
    const cards = screen.getAllByRole('article')
    expect(cards).toHaveLength(3)
    const [cafe, park, gig] = cards
    expect(within(cafe).getByRole('heading', { name: 'Courtyard Filter Coffee House' })).toBeInTheDocument()
    expect(within(cafe).getByText('3:00 PM')).toBeInTheDocument()
    expect(within(cafe).getByText('to 4:00 PM')).toBeInTheDocument()
    expect(within(cafe).getByText('Stop 1 · Café')).toBeInTheDocument()
    expect(within(cafe).getByText('₹250–₹450')).toBeInTheDocument()
    expect(within(cafe).getByText('listed price')).toBeInTheDocument()
    expect(within(cafe).getByText('Live · Google Maps')).toBeInTheDocument()
    expect(within(cafe).getByText('Google Maps')).toBeInTheDocument()
    expect(within(cafe).getByText(/A quiet, seated start/)).toBeInTheDocument()

    expect(within(park).getByText('free')).toBeInTheDocument() // basis badge; no redundant "Free" amount
    expect(within(park).queryByText('Free')).not.toBeInTheDocument()
    expect(within(park).getAllByText('Curated · OpenStreetMap').length).toBeGreaterThan(0)
    expect(within(park).getAllByText('© OpenStreetMap contributors')[0].closest('a')).toHaveAttribute(
      'href',
      'https://www.openstreetmap.org/copyright',
    )
    expect(within(park).getByText('Plan B if it rains')).toBeInTheDocument()
    expect(within(park).getByText('Venkatappa Art Gallery')).toBeInTheDocument()
    expect(within(park).getByText('Kept the walk short because you said you\'re tired.')).toBeInTheDocument()
    expect(within(park).getByText('Opening hours of the Plan B are unknown.')).toBeInTheDocument()

    expect(within(gig).getByText('5:35 PM')).toBeInTheDocument()
    expect(within(gig).getByText('Stop 3 · Event')).toBeInTheDocument()
    expect(within(gig).getByText('₹499')).toBeInTheDocument()
    expect(within(gig).getByText('ticket price')).toBeInTheDocument()
    expect(within(gig).getByText('Web event · check before you go')).toBeInTheDocument()
    expect(within(gig).getByRole('link', { name: /^Event page on district\.in\s?\(opens in a new tab\)$/ })).toHaveAttribute(
      'href',
      'https://www.district.in/events/sunset-acoustic-set-test',
    )

    // legs between cards, labelled as estimates
    expect(screen.getByText('Walk')).toBeInTheDocument()
    expect(screen.getByText('Auto-rickshaw')).toBeInTheDocument()
    expect(screen.getByText(/16–20 min · 5\.2 km · ₹60–₹90 fare/)).toBeInTheDocument()
    expect(screen.getByText(/12–15 min · 0\.9 km · no fare/)).toBeInTheDocument()
    expect(screen.getAllByText('estimate').length).toBeGreaterThanOrEqual(2)

    // maps links are https only
    const mapsLinks = screen.getAllByRole('link', { name: /Open in Google Maps/ })
    expect(mapsLinks).toHaveLength(3)
    for (const a of mapsLinks) expect(a.getAttribute('href')).toMatch(/^https:\/\/www\.google\.com\/maps\//)
    expect(screen.getByRole('link', { name: /Open full route in Google Maps/ })).toHaveAttribute(
      'href',
      result.plan!.maps_route_url!,
    )

    // trade-offs, checks, assumptions and warnings
    // omissions say what was left out: server name, else the idea, else a neutral label, never a raw ID
    const omitted = screen.getByRole('region', { name: 'Trade-offs and what I left out' })
    const items = within(omitted).getAllByRole('listitem').map((li) => li.textContent)
    expect(items).toEqual([
      'Hoppy Yard Brewpub — Serves alcohol and is likely busy on Saturday evenings.',
      'street food market — Likely crowded at that hour.',
      'Another option — Not open long enough during your window.',
    ])
    expect(screen.queryByText(/gp:ChIJtest_brewpub|fx:unknown_place/)).not.toBeInTheDocument()
    const checks = screen.getByRole('region', { name: 'Checks' })
    expect(within(checks).getAllByText('Pass')).toHaveLength(4)
    expect(within(checks).getByText('Warning')).toBeInTheDocument()
    expect(within(checks).getByText('Unverified')).toBeInTheDocument()
    expect(within(checks).getByText('Crowds (heuristic)')).toBeInTheDocument()
    expect(screen.getByText('Prices are per person')).toBeInTheDocument()
    expect(screen.getByText('Start time defaulted to 3:00 PM on the coming Saturday')).toBeInTheDocument()
    expect(screen.getByText('Carry cash for the auto-rickshaw')).toBeInTheDocument()
    expect(screen.getByText('Light rain is possible after sunset.')).toBeInTheDocument()

    // actions
    expect(screen.getByRole('button', { name: /Copy plan/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Copy share link/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Add to calendar/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Share on WhatsApp/ }).getAttribute('href')).toMatch(/^https:\/\/wa\.me\/\?text=/)
    expect(screen.getByRole('button', { name: /Yes/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Not really/ })).toBeInTheDocument()
  })

  it('shows the fallback banner with the degradation reason', () => {
    const result = fixtures.fallback()
    result.degradations = ['The AI planner timed out; used the deterministic planner', 'Weather not checked']
    render(<PlanView result={result} />)
    expect(screen.getByText('This is a backup plan')).toBeInTheDocument()
    expect(screen.getByText('The AI planner timed out; used the deterministic planner')).toBeInTheDocument()
    // data problems get their own notice; the model timeout is not called "reduced data"
    expect(screen.getByText('Running with reduced data')).toBeInTheDocument()
    expect(screen.getByText('Weather not checked')).toBeInTheDocument()
    expect(screen.queryByText('The rule-based planner took over')).not.toBeInTheDocument()
    expect(screen.getByText('Built by the backup planner (no AI model) and checked by the validator.')).toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(2)
  })

  it('pairs each leg with its stops by ID even when the legs arrive out of order', () => {
    const result = fixtures.complete()
    const plan = result.plan!
    plan.legs = [...plan.legs].reverse() // [park->gig auto, cafe->park walk]
    render(<PlanView result={result} />)
    const items = screen.getAllByRole('article').map((a) => a.closest('li') as HTMLElement)
    expect(within(items[0]).getByText('Walk')).toBeInTheDocument()
    expect(within(items[0]).queryByText('Auto-rickshaw')).not.toBeInTheDocument()
    expect(within(items[1]).getByText('Auto-rickshaw')).toBeInTheDocument()
    expect(within(items[2]).queryByText('Walk')).not.toBeInTheDocument()
    expect(within(items[2]).queryByText('Auto-rickshaw')).not.toBeInTheDocument()
  })

  it('labels fixture data as test data', () => {
    const result = fixtures.complete()
    result.plan!.stops[0].candidate.source = { provider: 'fixture' }
    render(<PlanView result={result} />)
    expect(screen.getByText('Test data')).toBeInTheDocument()
  })
})

describe('ClarificationCard', () => {
  const questions = fixtures.clarification().questions

  it('submits the chosen quick reply', () => {
    const onSubmit = vi.fn()
    render(<ClarificationCard questions={questions} onSubmit={onSubmit} />)
    const submit = screen.getByRole('button', { name: 'Continue planning' })
    expect(submit).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Mumbai' }))
    fireEvent.click(screen.getByRole('button', { name: 'Bangalore' }))
    expect(screen.getByRole('button', { name: 'Bangalore' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Mumbai' })).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(submit)
    expect(onSubmit).toHaveBeenCalledWith({ city: 'Bangalore' })
  })

  it('prefers a typed answer over a quick reply', () => {
    const onSubmit = vi.fn()
    render(<ClarificationCard questions={questions} onSubmit={onSubmit} />)
    fireEvent.click(screen.getByRole('button', { name: 'Delhi' }))
    fireEvent.change(screen.getByLabelText('Or type your own answer'), { target: { value: 'Pune' } })
    fireEvent.click(screen.getByRole('button', { name: 'Continue planning' }))
    expect(onSubmit).toHaveBeenCalledWith({ city: 'Pune' })
  })
})

describe('InfeasibleCard', () => {
  it('shows the reason, binding constraints and applies a relaxation', () => {
    const result = fixtures.infeasible()
    const onApply = vi.fn()
    render(<InfeasibleCard report={result.infeasible} message={result.message} currency="INR" onApply={onApply} />)
    expect(screen.getByRole('heading', { name: 'No plan fits everything' })).toBeInTheDocument()
    expect(screen.getByText(/costs more than ₹300/)).toBeInTheDocument()
    expect(screen.getByText('Budget')).toBeInTheDocument()
    expect(screen.getByText('Vegetarian')).toBeInTheDocument()
    expect(screen.getByText('₹850')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Raise budget to ₹850' }))
    expect(onApply).toHaveBeenCalledWith({ label: 'Raise budget to ₹850', patch: { budget: 850 } })
  })
})

describe('ChecksPanel', () => {
  it("labels every check name the backend emits (summarize_checks) and humanizes anything else", () => {
    // backend/app/domain/validator.py summarize_checks: one item per name, in this order.
    const names = ['budget', 'time', 'opening_hours', 'diet', 'accessibility', 'crowds', 'weather', 'sources', 'new_check']
    const checks: CheckItem[] = names.map((name) => ({ name, status: 'pass', detail: `${name} detail` }))
    render(<ChecksPanel checks={checks} />)
    const labels = within(screen.getByRole('region', { name: 'Checks' }))
      .getAllByRole('listitem')
      .map((li) => li.querySelector('.font-medium')?.textContent)
    expect(labels).toEqual([
      'Budget',
      'Time',
      'Opening hours',
      'Diet',
      'Accessibility',
      'Crowds (heuristic)',
      'Weather',
      'Sources',
      'New check',
    ])
  })
})

describe('PlanActions: Add to calendar', () => {
  const plan = fixtures.complete().plan!

  const originals = {
    create: Object.getOwnPropertyDescriptor(URL, 'createObjectURL'),
    revoke: Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL'),
  }

  function stubUrl(name: 'createObjectURL' | 'revokeObjectURL', value: unknown) {
    Object.defineProperty(URL, name, { value, configurable: true, writable: true })
  }

  afterEach(() => {
    vi.restoreAllMocks()
    for (const [name, desc] of [
      ['createObjectURL', originals.create],
      ['revokeObjectURL', originals.revoke],
    ] as const) {
      if (desc) Object.defineProperty(URL, name, desc)
      else delete (URL as unknown as Record<string, unknown>)[name]
    }
  })

  function clickCalendar() {
    render(<PlanActions plan={plan} shareUrl={null} shareId="sh_test123" />)
    fireEvent.click(screen.getByRole('button', { name: /Add to calendar/ }))
    return screen.getByRole('status')
  }

  it('confirms the download only when the file was handed to the browser', () => {
    const create = vi.fn(() => 'blob:calendar')
    stubUrl('createObjectURL', create)
    stubUrl('revokeObjectURL', vi.fn())
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    expect(clickCalendar()).toHaveTextContent('Calendar file downloaded.')
    expect(create).toHaveBeenCalledTimes(1)
    expect(click).toHaveBeenCalledTimes(1)
  })

  it('says so when the download fails instead of claiming success', () => {
    stubUrl('createObjectURL', () => {
      throw new Error('blocked')
    })
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const status = clickCalendar()
    expect(status).toHaveTextContent("Couldn't create the calendar file. Please try again.")
    expect(status).not.toHaveTextContent('Calendar file downloaded.')
  })
})
