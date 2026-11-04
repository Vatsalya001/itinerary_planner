import { describe, expect, it } from 'vitest'
import { fixtures } from '../test/utils'
import { HISTORY_KEY, HISTORY_LIMIT, HISTORY_TTL_MS, historyEntry, readHistory, writeHistory } from './itineraryHistory'

const NOW = Date.UTC(2026, 10, 4, 12)
const item = (i = 0) => ({ shareId: `sh_${i}`, city: 'Gurugram', date: '2026-11-07', prompt: 'Food and art', savedAt: NOW - i })
const storage = (value: unknown) => ({ getItem: () => JSON.stringify(value) })

describe('local itinerary history', () => {
  it('stores only selected metadata and the shared-plan ID, not venue records or resume tokens', () => {
    const result = fixtures.complete()
    result.thread_id = 'private-resume-token'
    const saved = historyEntry(result, { free_text: 'A relaxed outing in Gurugram' }, NOW)!
    expect(saved.city).toBe(result.plan!.city)
    expect(saved.prompt).toBe('A relaxed outing in Gurugram')
    expect(Object.keys(saved).sort()).toEqual(['city', 'date', 'prompt', 'savedAt', 'shareId'])
    expect(JSON.stringify(saved)).not.toContain('private-resume-token')
    expect(JSON.stringify(saved)).not.toContain('candidate')
  })
  it('handles structured requests and preserves a zero budget', () => {
    expect(historyEntry(fixtures.complete(), { city: 'Gurugram', budget: 0, currency: 'INR', interests: ['walks'] }, NOW)?.prompt)
      .toContain('Budget INR 0')
  })
  it('preserves structured overrides alongside the description', () => {
    const saved = historyEntry(fixtures.complete(), { free_text: 'Plan Bangalore', city: 'Gurugram', available_time: 120 }, NOW)!
    expect(saved.prompt).toContain('City: Gurugram')
    expect(saved.prompt).toContain('Time: 120 min')
    expect(saved.prompt).toContain('Plan Bangalore')
  })
  it('only bookmarks completed or fallback plans with a server share link', () => {
    expect(historyEntry(fixtures.clarification(), null, NOW)).toBeNull()
    expect(historyEntry(fixtures.infeasible(), null, NOW)).toBeNull()
    expect(historyEntry(fixtures.rejected(), null, NOW)).toBeNull()
    const result = fixtures.complete(); result.share_id = null
    expect(historyEntry(result, null, NOW)).toBeNull()
    expect(historyEntry(fixtures.fallback(), null, NOW)).not.toBeNull()
  })
  it('deduplicates, sorts newest first and caps retained history', () => {
    const entries = [item(2), item(1), item(0), item(0), ...Array.from({ length: 40 }, (_, i) => item(i + 3))]
    const read = readHistory(storage({ version: 1, entries }), NOW)
    expect(read.entries).toHaveLength(HISTORY_LIMIT)
    expect(read.entries[0].shareId).toBe('sh_0')
    expect(new Set(read.entries.map(e => e.shareId)).size).toBe(HISTORY_LIMIT)
  })
  it('expires metadata after 30 days and rejects future or invalid entries', () => {
    const entries = [item(), { ...item(1), savedAt: NOW - HISTORY_TTL_MS },
      { ...item(2), savedAt: NOW + 400_000 }, { ...item(3), date: '2026-02-31' },
      { ...item(4), shareId: 'javascript:alert(1)' }, { ...item(5), prompt: 'x'.repeat(1001) }, null]
    expect(readHistory(storage({ version: 1, entries }), NOW).entries).toEqual([item()])
  })
  it('ignores malicious extra fields and only constructs local routes from validated IDs', () => {
    const read = readHistory(storage({ version: 1, entries: [{ ...item(), href: 'https://evil.example', token: 'secret' }] }), NOW)
    expect(read.entries).toEqual([item()])
  })
  it.each(['{bad', JSON.stringify({ version: 99, entries: [] }), 'x'.repeat(250001)])('handles corrupt or oversized storage', raw => {
    const read = readHistory({ getItem: () => raw }, NOW)
    expect(read.entries).toEqual([])
    expect(read.notice).toBeTruthy()
  })
  it('handles blocked reads and full writes without throwing or modifying unrelated keys', () => {
    expect(readHistory({ getItem: () => { throw new Error('blocked') } }, NOW).notice).toBeTruthy()
    expect(writeHistory({ setItem: () => { throw new Error('full') } }, [item()])).toContain('only in this tab')
    const seen: string[] = []
    expect(writeHistory({ setItem: key => { seen.push(key) } }, [item()])).toBeNull()
    expect(seen).toEqual([HISTORY_KEY])
  })
})
