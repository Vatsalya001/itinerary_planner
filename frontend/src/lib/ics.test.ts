// @vitest-environment node
import { describe, expect, it } from 'vitest'
import complete from '../test/fixtures/plan_complete.json'
import type { CanonicalPlan, PlanResult } from '../types'
import { buildIcs, escapeIcsText, foldIcsLine, icsFilename, icsLocalDateTime, utcOffsetFor } from './ics'

const plan = (complete as unknown as PlanResult).plan as CanonicalPlan
const NOW = new Date('2026-11-05T04:30:00Z')

function unfold(ics: string): string[] {
  return ics.replace(/\r\n /g, '').split('\r\n')
}

describe('buildIcs', () => {
  const ics = buildIcs(plan, { now: NOW, shareUrl: 'https://saturday.example/p/sh_test123', uidSeed: 'sh_test123' })
  const lines = unfold(ics)

  it('is a VCALENDAR with one VEVENT per stop and CRLF line endings', () => {
    expect(ics.startsWith('BEGIN:VCALENDAR\r\nVERSION:2.0\r\n')).toBe(true)
    expect(ics.endsWith('END:VCALENDAR\r\n')).toBe(true)
    expect(lines.filter((l) => l === 'BEGIN:VEVENT')).toHaveLength(3)
    expect(lines.filter((l) => l === 'END:VEVENT')).toHaveLength(3)
    for (const physical of ics.split('\r\n')) {
      expect(physical).not.toMatch(/[\r\n]/)
    }
  })

  it('uses local times with the plan timezone as TZID', () => {
    expect(lines).toContain('DTSTART;TZID=Asia/Kolkata:20261107T150000')
    expect(lines).toContain('DTEND;TZID=Asia/Kolkata:20261107T160000')
    expect(lines).toContain('DTSTART;TZID=Asia/Kolkata:20261107T173500')
    expect(lines).toContain('DTEND;TZID=Asia/Kolkata:20261107T183500')
    expect(lines).toContain('TZID:Asia/Kolkata')
    expect(lines).toContain('TZOFFSETTO:+0530')
    expect(lines).toContain('DTSTAMP:20261105T043000Z')
  })

  it('escapes text and includes cost basis, source and links', () => {
    const summary = lines.find((l) => l.startsWith('SUMMARY:Cubbon'))
    expect(summary).toBe('SUMMARY:Cubbon Park loop (Park)')
    const location = lines.find((l) => l.startsWith('LOCATION:Courtyard'))
    expect(location).toContain('Courtyard Filter Coffee House'); expect(location).toContain('Bangalore')
    const desc = lines.find((l) => l.startsWith('DESCRIPTION:A quiet')) ?? ''
    expect(desc).toContain('\\nCost per person: ₹250–₹450 (listed price)')
    expect(desc).toContain('\\nSource: Live · Google Maps')
    expect(desc).toContain('\\nFull plan: https://saturday.example/p/sh_test123')
    expect(lines.some((l) => l.startsWith('URL:https://www.google.com/maps/'))).toBe(true)
    expect(lines.some((l) => l.startsWith('UID:sh_test123-1-'))).toBe(true)
  })

  it('folds long lines at 75 octets', () => {
    const encoder = new TextEncoder()
    for (const physical of ics.split('\r\n')) {
      expect(encoder.encode(physical).length).toBeLessThanOrEqual(75)
    }
  })

  it('falls back to floating local times for an unknown timezone', () => {
    const out = buildIcs({ ...plan, timezone: 'Not/AZone' }, { now: NOW })
    expect(out).not.toContain('TZID')
    expect(unfold(out)).toContain('DTSTART:20261107T150000')
  })

  it('names the file after the city and date', () => {
    expect(icsFilename(plan)).toBe('saturday-bangalore-2026-11-07.ics')
  })
})

describe('ics helpers', () => {
  it('escapes RFC 5545 text', () => {
    expect(escapeIcsText('a,b;c\\d\ne\r\nf')).toBe('a\\,b\\;c\\\\d\\ne\\nf')
  })

  it('rolls minutes past midnight into the next day', () => {
    expect(icsLocalDateTime('2026-11-07', 1530)).toBe('20261108T013000')
    expect(icsLocalDateTime('2026-12-31', 1440)).toBe('20270101T000000')
  })

  it('folds without splitting multi-byte characters', () => {
    const long = `DESCRIPTION:${'₹'.repeat(60)}`
    const folded = foldIcsLine(long)
    expect(folded.replace(/\r\n /g, '')).toBe(long)
    const encoder = new TextEncoder()
    for (const physical of folded.split('\r\n')) expect(encoder.encode(physical).length).toBeLessThanOrEqual(75)
  })

  it('computes UTC offsets', () => {
    expect(utcOffsetFor('Asia/Kolkata', '2026-11-07')).toBe('+0530')
    expect(utcOffsetFor('UTC', '2026-11-07')).toBe('+0000')
    expect(utcOffsetFor('America/New_York', '2026-11-07')).toBe('-0500')
    expect(utcOffsetFor('Not/AZone', '2026-11-07')).toBeNull()
  })
})
