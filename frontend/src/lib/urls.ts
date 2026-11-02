import type { Candidate } from '../types'

/**
 * Returns a normalised URL only if it is an absolute https: URL without embedded credentials.
 * Everything else (javascript:, data:, http:, relative paths, garbage) returns null and is not rendered.
 */
export function safeHttpsUrl(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const trimmed = raw.trim()
  if (!trimmed) return null
  let url: URL
  try {
    url = new URL(trimmed)
  } catch {
    return null
  }
  if (url.protocol !== 'https:') return null
  if (url.username || url.password) return null
  return url.href
}

/** Link for "Open in Google Maps": the provider's https URL, else a keyless Maps search URL we build. */
export function mapsUrlFor(candidate: Candidate, city?: string): string {
  const provided = safeHttpsUrl(candidate.maps_url)
  if (provided) return provided
  const query = [candidate.name, candidate.address ?? city].filter((x): x is string => Boolean(x)).join(', ')
  const params = new URLSearchParams({ api: '1', query })
  if (candidate.id.startsWith('gp:')) params.set('query_place_id', candidate.id.slice(3))
  return `https://www.google.com/maps/search/?${params.toString()}`
}

/** Absolute URL of a shared plan page. */
export function shareUrlFor(shareId: string, origin: string = window.location.origin): string {
  return `${origin}/p/${encodeURIComponent(shareId)}`
}
