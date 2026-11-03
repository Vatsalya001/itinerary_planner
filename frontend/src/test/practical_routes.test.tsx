import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PlanView } from '../components/PlanView'
import { fmtCandidateCost } from '../lib/format'
import { planToText } from '../lib/share'
import { routeUrlFor } from '../lib/urls'
import { fixtures } from './utils'

function walkingPlan() {
  const result = fixtures.complete()
  const plan = result.plan!
  plan.stops = plan.stops.slice(0, 2)
  plan.legs = [{ ...plan.legs[0], from_id: plan.stops[0].candidate.id, to_id: plan.stops[1].candidate.id, mode: 'walk' }]
  plan.maps_route_url = 'https://www.google.com/maps/dir/?api=1&travelmode=driving'
  return result
}

describe('practical route and evidence regressions', () => {
  it('corrects stale shared driving links for walking itineraries and copied text', () => {
    const result = walkingPlan()
    expect(new URL(routeUrlFor(result.plan!)!).searchParams.get('travelmode')).toBe('walking')
    expect(planToText(result.plan!)).toContain('travelmode=walking')
    expect(planToText(result.plan!)).not.toContain('travelmode=driving')
  })

  it('does not invent one mode for a mixed-mode route', () => {
    const plan = fixtures.complete().plan!
    plan.legs[0].mode = 'walk'
    plan.legs[1].mode = 'cab'
    expect(routeUrlFor(plan)).toBeNull()
    expect(new URL(routeUrlFor(plan, plan.legs[0])!).searchParams.get('travelmode')).toBe('walking')
    expect(new URL(routeUrlFor(plan, plan.legs[1])!).searchParams.get('travelmode')).toBe('driving')
  })

  it('shows entrance evidence scope and does not call wheelchair transfers a verified walk', () => {
    const result = walkingPlan()
    result.preferences!.hard_constraints = [{ kind: 'access', value: 'wheelchair', source_text: 'wheelchair' }]
    result.plan!.stops.forEach((s) => { s.candidate.wheelchair = 'confirmed' })
    render(<PlanView result={result} />)
    expect(screen.getAllByText('Source reports a wheelchair-accessible entrance.')).toHaveLength(2)
    expect(screen.getAllByText(/Internal facilities, toilets and routes between stops are unverified/)).toHaveLength(2)
    expect(screen.getByText('Outdoor transfer')).toBeInTheDocument()
    expect(screen.getByText(/standard pedestrian estimate/)).toBeInTheDocument()
    expect(screen.getByText(/timing is not a wheelchair-route estimate/)).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /Check entrance evidence/ })).toHaveLength(2)
    expect(screen.getByRole('link', { name: /Open full route/ }).getAttribute('href')).toContain('travelmode=walking')
  })

  it('does not present a missing zero-default price as free admission', () => {
    const candidate = walkingPlan().plan!.stops[0].candidate
    candidate.cost = { min: 0, max: 0, currency: 'GBP', basis: 'category_estimate' }
    expect(fmtCandidateCost(candidate)).toBe('Price unknown (not included in total)')
    candidate.cost.basis = 'free'
    expect(fmtCandidateCost(candidate)).toBe('Free')
  })

  it('clearly separates a partial schedule from the requested window', () => {
    const result = walkingPlan()
    result.plan!.totals.elapsed_min = 63
    result.plan!.totals.available_minutes = 240
    render(<PlanView result={result} />)
    expect(screen.getByText('Partial itinerary')).toBeInTheDocument()
    expect(screen.getByText(/Requested window: 3:00 PM – 7:00 PM/)).toBeInTheDocument()
  })

  it('copied wheelchair plans do not label the transfer as a verified walk', () => {
    const text = planToText(walkingPlan().plan!, { accessRequired: true })
    expect(text).toContain('outdoor transfer (step-free route unverified)')
    expect(text).toContain('standard pedestrian estimate')
    expect(text).not.toContain('then walk about')
  })

  it('retains price and access caveats in copied and WhatsApp text', () => {
    const plan = walkingPlan().plan!
    plan.stops[0].tradeoff = 'Admission cost is unverified.'
    plan.stops[0].warnings = ['Confirm step-free routes before travelling.']
    const text = planToText(plan)
    expect(text).toContain('Note: Admission cost is unverified.')
    expect(text).toContain('Verify: Confirm step-free routes before travelling.')
  })
})
