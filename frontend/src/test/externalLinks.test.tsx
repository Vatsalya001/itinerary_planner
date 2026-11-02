import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { NEW_TAB_HINT } from '../components/ExternalLink'
import { PlanView } from '../components/PlanView'
import { Layout } from '../components/Layout'
import HowItWorks from '../pages/HowItWorks'
import { fixtures } from './utils'

// Every source file in src/, as text (Vite's raw import), to enforce the rule statically as well.
const SOURCES = import.meta.glob(['../**/*.{ts,tsx}', '!../**/*.test.{ts,tsx}'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const NEW_TAB = /target\s*[=:]\s*\{?\s*["'`]_blank|window\.open\s*\(/

describe('links that open a new tab', () => {
  it('are only created by ExternalLink, which always adds the hint', () => {
    const files = Object.keys(SOURCES)
    expect(files.length).toBeGreaterThan(20) // the glob really saw the source tree
    const offenders = files.filter((f) => !f.endsWith('/components/ExternalLink.tsx') && NEW_TAB.test(SOURCES[f]))
    expect(offenders).toEqual([])
  })

  it('announce "(opens in a new tab)" and use rel="noopener noreferrer" wherever they render', () => {
    const result = fixtures.complete() // Google, OpenStreetMap snapshot (stop, Plan B, footer) and web-event links
    const { container } = render(
      <MemoryRouter>
        <PlanView result={result} shareUrl="https://saturday.example/p/sh_test123" />
        <HowItWorks />
      </MemoryRouter>,
    )
    const links = Array.from(container.querySelectorAll<HTMLAnchorElement>('a[target="_blank"]'))
    // 3 maps links + Plan B maps link + 2 OSM (Plan B, stop) + OSM footer + event page + route + WhatsApp
    expect(links.length).toBeGreaterThanOrEqual(10)
    for (const a of links) {
      // jsdom's name algorithm trims the hidden span's leading space, so allow both "x (opens…)" and "x(opens…)".
      expect(a).toHaveAccessibleName(expect.stringMatching(/\(opens in a new tab\)$/))
      const hint = a.querySelector(':scope > span.sr-only:last-child')
      expect(hint?.textContent).toBe(` ${NEW_TAB_HINT}`)
      expect(a.getAttribute('rel')?.split(' ')).toEqual(expect.arrayContaining(['noopener', 'noreferrer']))
      expect(a.getAttribute('href')).toMatch(/^https:\/\//)
    }
    expect(NEW_TAB_HINT).toBe('(opens in a new tab)')
  })

  it('the layout has no new-tab links without the hint', () => {
    const { container } = render(
      <MemoryRouter>
        <Layout />
      </MemoryRouter>,
    )
    for (const a of Array.from(container.querySelectorAll('a[target="_blank"]'))) {
      expect(a).toHaveAccessibleName(expect.stringMatching(/\(opens in a new tab\)$/))
    }
  })
})
