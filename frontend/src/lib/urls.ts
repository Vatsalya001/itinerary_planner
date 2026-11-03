import type { Candidate, CanonicalPlan, Leg } from '../types'

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

/** Recompute directions from actual legs, including old shared records with stale driving URLs. */
export function routeUrlFor(plan: CanonicalPlan, leg?: Leg): string | null {
  const modes = new Set((leg ? [leg] : plan.legs).map((l) => l.mode))
  if (modes.size > 1) return null // one Maps URL cannot express mixed transport modes
  const stops = leg
    ? [plan.stops.find((s) => s.candidate.id === leg.from_id), plan.stops.find((s) => s.candidate.id === leg.to_id)]
    : plan.stops
  if (!stops.length || stops.some((s) => !s || !Number.isFinite(s.candidate.lat) || !Number.isFinite(s.candidate.lng))) return null
  const points = stops.map((s) => `${s!.candidate.lat.toFixed(6)},${s!.candidate.lng.toFixed(6)}`)
  const params = new URLSearchParams({ api: '1', destination: points[points.length - 1] })
  if (points.length > 1) params.set('origin', points[0])
  if (points.length > 2) params.set('waypoints', points.slice(1, -1).join('|'))
  const mode = [...modes][0]
  params.set('travelmode', mode === 'walk' ? 'walking' : mode === 'metro' ? 'transit' : 'driving')
  return `https://www.google.com/maps/dir/?${params.toString()}`
}
