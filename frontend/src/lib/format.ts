import type { Candidate, CostBasis, ProviderName, TravelMode } from '../types'

const MINUTES_PER_DAY = 1440

/**
 * Minutes since local midnight -> "3:00 PM". Values past midnight get a day suffix: 1530 -> "1:30 AM +1".
 */
export function fmtClock(minutes: number): string {
  if (!Number.isFinite(minutes)) return '--:--'
  const m = Math.round(minutes)
  const dayOffset = Math.floor(m / MINUTES_PER_DAY)
  const inDay = ((m % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY
  const h24 = Math.floor(inDay / 60)
  const mm = inDay % 60
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12
  const base = `${h12}:${String(mm).padStart(2, '0')} ${h24 < 12 ? 'AM' : 'PM'}`
  if (dayOffset > 0) return `${base} +${dayOffset}`
  if (dayOffset < 0) return `${base} ${dayOffset}`
  return base
}

/** "3:00 PM – 7:00 PM" */
export function fmtClockRange(start: number, end: number): string {
  return `${fmtClock(start)} – ${fmtClock(end)}`
}

/** 45 -> "45 min", 60 -> "1 h", 90 -> "1 h 30 min". */
export function fmtDuration(minutes: number): string {
  if (!Number.isFinite(minutes)) return '—'
  const m = Math.max(0, Math.round(minutes))
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  const rest = m % 60
  return rest === 0 ? `${h} h` : `${h} h ${rest} min`
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  INR: '₹',
  GBP: '£',
  USD: '$',
  EUR: '€',
  JPY: '¥',
  SGD: 'S$',
  AUD: 'A$',
  CAD: 'C$',
}

export function currencySymbol(currency: string): string {
  return CURRENCY_SYMBOLS[currency.toUpperCase()] ?? `${currency.toUpperCase()} `
}

/** Group digits: Indian style (12,34,567) or western (1,234,567). */
export function groupDigits(digits: string, indian: boolean): string {
  if (digits.length <= 3) return digits
  const last3 = digits.slice(-3)
  const head = digits.slice(0, -3)
  const size = indian ? 2 : 3
  const groups: string[] = []
  for (let end = head.length; end > 0; end -= size) {
    groups.unshift(head.slice(Math.max(0, end - size), end))
  }
  return `${groups.join(',')},${last3}`
}

/** Digit grouping for a currency: Indian (en-IN, 1,00,000) for INR, western (en-US/en-GB, 100,000) otherwise. */
function indianGrouping(currency: string | null | undefined): boolean {
  return (currency || '').trim().toUpperCase() === 'INR'
}

/** A whole amount without a symbol, grouped for the currency: 1000000 -> "10,00,000" (INR), "1,000,000" (others). */
export function fmtAmount(amount: number, currency: string | null | undefined): string {
  if (!Number.isFinite(amount)) return '—'
  const rounded = Math.round(amount)
  return `${rounded < 0 ? '-' : ''}${groupDigits(String(Math.abs(rounded)), indianGrouping(currency))}`
}

/** Whole currency units -> "₹2,000", "₹1,00,000", "£25", "AED 200". */
export function fmtMoney(amount: number, currency: string): string {
  if (!Number.isFinite(amount)) return '—'
  const rounded = Math.round(amount)
  const code = (currency || '').toUpperCase()
  return `${rounded < 0 ? '-' : ''}${currencySymbol(code || '?')}${fmtAmount(Math.abs(rounded), code)}`
}

/** Cost range -> "Free", "₹300", "₹200–₹400". */
export function fmtMoneyRange(min: number, max: number, currency: string): string {
  if (max <= 0) return 'Free'
  if (min === max) return fmtMoney(max, currency)
  return `${fmtMoney(min, currency)}–${fmtMoney(max, currency)}`
}

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const MONTHS = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

/** Parse "YYYY-MM-DD" into calendar parts without timezone drift. */
export function parseIsoDate(iso: string): { year: number; month: number; day: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso)
  if (!m) return null
  return { year: Number(m[1]), month: Number(m[2]), day: Number(m[3]) }
}

/** "2026-11-07" -> "Saturday, 7 November 2026" */
export function fmtDate(iso: string): string {
  const p = parseIsoDate(iso)
  if (!p) return iso
  const weekday = new Date(Date.UTC(p.year, p.month - 1, p.day)).getUTCDay()
  return `${WEEKDAYS[weekday]}, ${p.day} ${MONTHS[p.month - 1]} ${p.year}`
}

/** "2026-11-07" -> "Sat 7 Nov" */
export function fmtShortDate(iso: string): string {
  const p = parseIsoDate(iso)
  if (!p) return iso
  const weekday = new Date(Date.UTC(p.year, p.month - 1, p.day)).getUTCDay()
  return `${WEEKDAYS[weekday].slice(0, 3)} ${p.day} ${MONTHS[p.month - 1].slice(0, 3)}`
}

export function humanize(text: string): string {
  const t = text.replace(/[_-]+/g, ' ').trim()
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : t
}

const CATEGORY_LABELS: Record<string, string> = {
  cafe: 'Café',
  street_food: 'Street food',
  live_music_venue: 'Live music',
  game_arcade: 'Arcade',
  art_gallery: 'Art gallery',
  place_of_worship: 'Place of worship',
  heritage_walk: 'Heritage walk',
}

export function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? humanize(category)
}

export const COST_BASIS_LABELS: Record<CostBasis, string> = {
  listed_price_range: 'listed price',
  price_level_estimate: 'price level estimate',
  ticket_price: 'ticket price',
  free: 'free',
  category_estimate: 'estimate',
}

export function costBasisLabel(basis: string): string {
  return (COST_BASIS_LABELS as Record<string, string>)[basis] ?? 'estimate'
}

export const SOURCE_LABELS: Record<ProviderName, string> = {
  google_places: 'Live · Google Maps',
  curated_snapshot: 'Curated · OpenStreetMap',
  web_event: 'Web event · check before you go',
  fixture: 'Test data',
  fixture_event: 'Test data',
}

export function sourceLabel(provider: string): string {
  return (SOURCE_LABELS as Record<string, string>)[provider] ?? 'Unverified source'
}

export const MODE_LABELS: Record<TravelMode, string> = {
  walk: 'Walk',
  auto: 'Auto-rickshaw',
  cab: 'Cab',
  metro: 'Metro',
}

export function modeLabel(mode: string): string {
  return (MODE_LABELS as Record<string, string>)[mode] ?? humanize(mode)
}

/** Ratio 0..1 -> "87%". Values above 1 are treated as already being a percentage. */
export function fmtPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const pct = value <= 1 ? value * 100 : value
  return `${Math.round(pct)}%`
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`
}

interface WeatherLike {
  status: string
  summary?: string | null
  precip_prob_max?: number | null
  temp_min_c?: number | null
  temp_max_c?: number | null
  sunset_min?: number | null
}

/** Temperature, rain chance and sunset, without the summary text. */
export function weatherDetails(w: WeatherLike): string {
  if (w.status !== 'ok') return ''
  const parts: string[] = []
  if (typeof w.temp_max_c === 'number') {
    parts.push(
      typeof w.temp_min_c === 'number'
        ? `${Math.round(w.temp_min_c)}–${Math.round(w.temp_max_c)}°C`
        : `up to ${Math.round(w.temp_max_c)}°C`,
    )
  }
  if (typeof w.precip_prob_max === 'number') parts.push(`${w.precip_prob_max}% chance of rain`)
  if (typeof w.sunset_min === 'number') parts.push(`sunset ${fmtClock(w.sunset_min)}`)
  return parts.join(' · ')
}

/** One line describing the weather, e.g. "Partly cloudy · 19–28°C · 20% chance of rain · sunset 5:52 PM". */
export function weatherLine(w: WeatherLike): string {
  if (w.status !== 'ok') return 'Weather not checked'
  const extra = weatherDetails({ ...w, precip_prob_max: /chance of rain/i.test(w.summary ?? '') ? null : w.precip_prob_max,
    temp_max_c: /°/.test(w.summary ?? '') ? null : w.temp_max_c })
  const parts = [w.summary, extra].filter((x): x is string => Boolean(x))
  return parts.length ? parts.join(' · ') : 'Weather checked'
}

/** A generic zero default means missing venue pricing, not verified free admission. */
export function fmtCandidateCost(candidate: Candidate): string {
  const cost = candidate.cost
  return cost.max === 0 && cost.basis === 'category_estimate'
    ? 'Price unknown (not included in total)'
    : fmtMoneyRange(cost.min, cost.max, cost.currency)
}
