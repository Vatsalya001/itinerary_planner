import type { CanonicalPlan } from '../types'
import { categoryLabel, costBasisLabel, fmtMoneyRange, parseIsoDate, sourceLabel } from './format'
import { mapsUrlFor, safeHttpsUrl } from './urls'

export interface IcsOptions {
  /** DTSTAMP; injectable for tests. */
  now?: Date
  /** Link back to the shared plan, added to each description. */
  shareUrl?: string | null
  /** Stable part of each UID (e.g. the share ID). */
  uidSeed?: string | null
}

const CRLF = '\r\n'

/** RFC 5545 TEXT escaping: backslash, semicolon, comma and newlines. Control characters are dropped. */
export function escapeIcsText(text: string): string {
  return text
    .replace(/\r\n?/g, '\n')
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '')
    .replace(/\\/g, '\\\\')
    .replace(/;/g, '\\;')
    .replace(/,/g, '\\,')
    .replace(/\n/g, '\\n')
}

const encoder = new TextEncoder()

/** Fold a content line to at most 75 octets per physical line (continuations start with a space). */
export function foldIcsLine(line: string): string {
  if (encoder.encode(line).length <= 75) return line
  const out: string[] = []
  let current = ''
  let currentBytes = 0
  let limit = 75
  for (const ch of line) {
    const size = encoder.encode(ch).length
    if (currentBytes + size > limit) {
      out.push(current)
      current = ''
      currentBytes = 0
      limit = 74 // the leading space of a continuation line counts toward 75
    }
    current += ch
    currentBytes += size
  }
  out.push(current)
  return out.join(`${CRLF} `)
}

function pad(n: number, width = 2): string {
  return String(n).padStart(width, '0')
}

/** Plan date + minutes since local midnight (may exceed 1440) -> "YYYYMMDDTHHMMSS" local time. */
export function icsLocalDateTime(isoDate: string, minutes: number): string {
  const p = parseIsoDate(isoDate)
  if (!p) throw new Error(`invalid plan date: ${isoDate}`)
  const d = new Date(Date.UTC(p.year, p.month - 1, p.day, 0, Math.round(minutes)))
  return (
    `${pad(d.getUTCFullYear(), 4)}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}00`
  )
}

function icsUtc(d: Date): string {
  return (
    `${pad(d.getUTCFullYear(), 4)}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`
  )
}

/** UTC offset of `timeZone` at noon on the plan date, as "+0530"; null if the zone is unknown. */
export function utcOffsetFor(timeZone: string, isoDate: string): string | null {
  const p = parseIsoDate(isoDate)
  if (!p) return null
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone, timeZoneName: 'longOffset' }).formatToParts(
      new Date(Date.UTC(p.year, p.month - 1, p.day, 12)),
    )
    const name = parts.find((x) => x.type === 'timeZoneName')?.value ?? ''
    if (name === 'GMT' || name === 'UTC') return '+0000'
    const m = /([+-])(\d{1,2})(?::?(\d{2}))?/.exec(name)
    if (!m) return null
    return `${m[1]}${pad(Number(m[2]))}${m[3] ?? '00'}`
  } catch {
    return null
  }
}

/** Build an iCalendar file with one VEVENT per stop, in the plan's local time zone. */
export function buildIcs(plan: CanonicalPlan, options: IcsOptions = {}): string {
  const now = options.now ?? new Date()
  const offset = plan.timezone ? utcOffsetFor(plan.timezone, plan.date) : null
  const tzid = offset ? plan.timezone : null
  const seed = (options.uidSeed ?? `${plan.date}-${plan.city}`).replace(/[^A-Za-z0-9._-]+/g, '-')
  const shareUrl = safeHttpsUrl(options.shareUrl)

  const lines: string[] = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    'PRODID:-//Perfect Saturday Planner//EN',
    'CALSCALE:GREGORIAN',
    'METHOD:PUBLISH',
    `X-WR-CALNAME:${escapeIcsText(`Saturday in ${plan.city}`)}`,
  ]
  if (tzid && offset) {
    // A minimal fixed-offset definition valid for the plan's date; clients that know the IANA zone use their own.
    lines.push(
      'BEGIN:VTIMEZONE',
      `TZID:${tzid}`,
      'BEGIN:STANDARD',
      'DTSTART:19700101T000000',
      `TZOFFSETFROM:${offset}`,
      `TZOFFSETTO:${offset}`,
      'END:STANDARD',
      'END:VTIMEZONE',
    )
  }

  const stamp = icsUtc(now)
  const dt = (minutes: number) =>
    tzid ? `;TZID=${tzid}:${icsLocalDateTime(plan.date, minutes)}` : `:${icsLocalDateTime(plan.date, minutes)}`

  plan.stops.forEach((stop, index) => {
    const c = stop.candidate
    const mapsUrl = mapsUrlFor(c, plan.city)
    const description: string[] = []
    if (stop.why) description.push(stop.why)
    if (stop.tradeoff) description.push(`Trade-off: ${stop.tradeoff}`)
    description.push(
      `Cost per person: ${fmtMoneyRange(c.cost.min, c.cost.max, c.cost.currency)} (${costBasisLabel(c.cost.basis)})`,
    )
    if (stop.plan_b) description.push(`Plan B if it rains: ${stop.plan_b.name}`)
    description.push(`Source: ${sourceLabel(c.source.provider)}`)
    description.push(`Map: ${mapsUrl}`)
    if (shareUrl) description.push(`Full plan: ${shareUrl}`)

    lines.push(
      'BEGIN:VEVENT',
      `UID:${seed}-${index + 1}-${c.id.replace(/[^A-Za-z0-9._-]+/g, '-')}@perfect-saturday`,
      `DTSTAMP:${stamp}`,
      `DTSTART${dt(stop.start_min)}`,
      `DTEND${dt(stop.end_min)}`,
      `SUMMARY:${escapeIcsText(`${c.name} (${categoryLabel(c.category)})`)}`,
      `LOCATION:${escapeIcsText(c.address || c.name)}`,
      `DESCRIPTION:${escapeIcsText(description.join('\n'))}`,
      `URL:${mapsUrl}`,
      'END:VEVENT',
    )
  })
  lines.push('END:VCALENDAR')
  return lines.map(foldIcsLine).join(CRLF) + CRLF
}

export function icsFilename(plan: CanonicalPlan): string {
  const slug = plan.city.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'plan'
  return `saturday-${slug}-${plan.date}.ics`
}

/** Trigger a browser download of the plan as an .ics file. */
export function downloadIcs(plan: CanonicalPlan, options: IcsOptions = {}): void {
  const blob = new Blob([buildIcs(plan, options)], { type: 'text/calendar;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = icsFilename(plan)
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
