import type { PlanRequest, PlanResult } from '../types'

export const HISTORY_KEY = 'saturday-planner:itinerary-history:v1'
export const HISTORY_LIMIT = 30
export const HISTORY_TTL_MS = 30 * 24 * 60 * 60 * 1000
const MAX_BYTES = 1_000_000

/** Bookmarks only: venue records, traces, resume tokens and credentials are never copied into localStorage. */
export interface ItineraryHistoryEntry {
  shareId: string
  city: string
  date: string
  prompt: string
  savedAt: number
}
export interface HistoryState { entries: ItineraryHistoryEntry[]; notice: string | null }

function entry(value: unknown, now: number): ItineraryHistoryEntry | null {
  if (!value || typeof value !== 'object') return null
  const v = value as Record<string, unknown>
  if (typeof v.shareId !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(v.shareId)
    || typeof v.city !== 'string' || !v.city.trim() || v.city.length > 120
    || typeof v.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v.date)
    || !Number.isFinite(Date.parse(`${v.date}T00:00:00Z`))
    || new Date(`${v.date}T00:00:00Z`).toISOString().slice(0, 10) !== v.date
    || typeof v.prompt !== 'string' || v.prompt.length > 4000
    || typeof v.savedAt !== 'number' || !Number.isFinite(v.savedAt)
    || v.savedAt > now + 300_000 || now - v.savedAt >= HISTORY_TTL_MS) return null
  return { shareId: v.shareId, city: v.city, date: v.date, prompt: v.prompt, savedAt: v.savedAt }
}

export function readHistory(storage: Pick<Storage, 'getItem'>, now = Date.now()): HistoryState {
  try {
    const raw = storage.getItem(HISTORY_KEY)
    if (!raw) return { entries: [], notice: null }
    if (raw.length > MAX_BYTES) throw new Error('oversized history')
    const data: unknown = JSON.parse(raw)
    if (!data || typeof data !== 'object' || !('version' in data) || data.version !== 1
      || !('entries' in data) || !Array.isArray(data.entries)) throw new Error('invalid history')
    const seen = new Set<string>()
    const entries = data.entries.slice(0, 100).map(v => entry(v, now))
      .filter((v): v is ItineraryHistoryEntry => v !== null)
      .sort((a, b) => b.savedAt - a.savedAt)
      .filter(v => { if (seen.has(v.shareId)) return false; seen.add(v.shareId); return true })
      .slice(0, HISTORY_LIMIT)
    return { entries, notice: null }
  } catch {
    return { entries: [], notice: 'Saved history could not be read. You can still plan normally.' }
  }
}

export function historyEntry(result: PlanResult, request: PlanRequest | null, now = Date.now()): ItineraryHistoryEntry | null {
  if (!['complete', 'fallback'].includes(result.status) || !result.plan || !result.share_id) return null
  const p = result.plan
  const details = [
    request?.city ? `City: ${request.city}` : '',
    request?.date ? `Date: ${request.date}` : '',
    request?.neighbourhood ? `Near: ${request.neighbourhood}` : '',
    request?.group_size ? `Group size: ${request.group_size}` : '',
    request?.start_time ? `Start: ${request.start_time}` : '',
    request?.available_time ? `Time: ${request.available_time}${typeof request.available_time === 'number' ? ' min' : ''}` : '',
    request?.budget !== undefined && request.budget !== null ? `Budget ${request.currency || result.preferences?.currency || ''} ${request.budget}` : '',
    request?.mood ? `Mood: ${request.mood}` : '',
    request?.interests?.length ? `Interests: ${request.interests.join(', ')}` : '',
    request?.constraints?.length ? `Constraints: ${request.constraints.join(', ')}` : '',
  ].filter(Boolean).join(' · ')
  // Form overrides precede the description so edited fields remain visible even for long prompts.
  const prompt = [details, request?.free_text?.trim()].filter(Boolean).join('\n') || p.city
  return entry({ shareId: result.share_id, city: p.city, date: p.date, prompt: prompt.slice(0, 4000), savedAt: now }, now)
}

export function writeHistory(storage: Pick<Storage, 'setItem'>, entries: ItineraryHistoryEntry[]): string | null {
  try {
    storage.setItem(HISTORY_KEY, JSON.stringify({ version: 1, entries: entries.slice(0, HISTORY_LIMIT) }))
    return null
  } catch {
    return 'Browser storage is unavailable or full. History changes will last only in this tab.'
  }
}
