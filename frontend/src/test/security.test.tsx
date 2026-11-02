import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PlanView } from '../components/PlanView'
import { PreferencesSummary } from '../components/PreferencesSummary'
import { fixtures } from './utils'

const XSS_MOOD = '<img src=x onerror="alert(1)">tired'
const XSS_WHY = '<script>alert("why")</script><b>bold</b> & more'

describe('output safety', () => {
  it('renders XSS strings in mood and why as literal text', () => {
    const result = fixtures.complete()
    result.preferences!.mood = XSS_MOOD
    result.plan!.stops[0].why = XSS_WHY
    result.plan!.stops[1].tradeoff = '<iframe src="https://evil.example"></iframe>'
    result.plan!.stops[2].candidate.name = '<svg onload=alert(2)>Gig'
    const { container } = render(
      <>
        <PreferencesSummary prefs={result.preferences!} />
        <PlanView result={result} />
      </>,
    )
    expect(screen.getByText(XSS_MOOD)).toBeInTheDocument()
    expect(screen.getByText(XSS_WHY)).toBeInTheDocument()
    expect(screen.getByText('<iframe src="https://evil.example"></iframe>')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '<svg onload=alert(2)>Gig' })).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('[onerror]')).toBeNull()
    expect(container.querySelector('[onload]')).toBeNull()
  })

  it('never renders javascript: (or other non-https) links', () => {
    const result = fixtures.complete()
    const plan = result.plan!
    plan.maps_route_url = 'javascript:alert(1)'
    plan.stops[0].candidate.maps_url = 'javascript:alert(2)'
    plan.stops[1].plan_b!.maps_url = 'data:text/html,<script>alert(3)</script>'
    plan.stops[2].candidate.source.url = ' JaVaScRiPt:alert(4)'
    plan.stops[2].candidate.maps_url = 'http://insecure.example/map'
    const { container } = render(<PlanView result={result} shareUrl="https://saturday.example/p/sh_test123" />)

    const hrefs = Array.from(container.querySelectorAll('a')).map((a) => a.getAttribute('href') ?? '')
    expect(hrefs.length).toBeGreaterThan(0)
    for (const href of hrefs) expect(href).toMatch(/^https:\/\//)
    expect(hrefs.some((h) => /javascript:|data:|http:\/\//i.test(h))).toBe(false)

    // the unsafe route link is dropped; unsafe place links fall back to a Maps search URL we build
    expect(screen.queryByRole('link', { name: /Open full route/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Event page/ })).not.toBeInTheDocument()
    expect(screen.getByText('Source: district.in')).toBeInTheDocument()
    for (const a of screen.getAllByRole('link', { name: /Open in Google Maps/ })) {
      expect(a.getAttribute('href')).toMatch(/^https:\/\/www\.google\.com\/maps\/search\/\?api=1&query=/)
    }
  })
})
