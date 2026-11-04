import type { CanonicalPlan } from '../types'
import {
  categoryLabel,
  costBasisLabel,
  fmtCandidateCost,
  fmtClockRange,
  fmtDate,
  fmtMoney,
  fmtMoneyRange,
  modeLabel,
  weatherLine,
} from './format'
import { legAfter } from './plan'
import { routeUrlFor, safeHttpsUrl } from './urls'

export interface ShareTextOptions {
  shareUrl?: string | null
  accessRequired?: boolean
}

/** The plan as plain text for copying or WhatsApp. Only data fields; no markup. */
export function planToText(plan: CanonicalPlan, options: ShareTextOptions = {}): string {
  const t = plan.totals
  const lines: string[] = []
  lines.push(`My Saturday in ${plan.city}`)
  lines.push(`${fmtDate(plan.date)}, ${fmtClockRange(plan.start_min, plan.end_min)}`)
  lines.push(`Requested window: ${fmtClockRange(plan.start_min, plan.start_min + t.available_minutes)}`)
  lines.push(`Weather: ${weatherLine(plan.weather)}`)
  lines.push('')

  plan.stops.forEach((stop, i) => {
    const c = stop.candidate
    lines.push(`${i + 1}. ${fmtClockRange(stop.start_min, stop.end_min)}  ${c.name} (${categoryLabel(c.category)})`)
    lines.push(`   ${fmtCandidateCost(c)} per person, ${costBasisLabel(c.cost.basis)}`)
    if (stop.why) lines.push(`   Why: ${stop.why}`)
    if (stop.tradeoff) lines.push(`   Note: ${stop.tradeoff}`)
    for (const warning of stop.warnings) lines.push(`   Verify: ${warning}`)
    for (const e of c.requirement_evidence ?? []) {
      lines.push(`   Facility ${e.description}: ${e.status}; confirm current availability with the venue`)
      const source = safeHttpsUrl(e.source_url)
      if (source) lines.push(`   Facility source: ${source}`)
    }
    if (stop.plan_b) lines.push(`   Plan B if it rains: ${stop.plan_b.name}`)
    const leg = legAfter(plan, i) // by from_id/to_id, exactly like the timeline in PlanView
    if (leg) {
      const fare = leg.fare.max > 0 ? `, ${fmtMoneyRange(leg.fare.min, leg.fare.max, leg.fare.currency)} estimate` : ''
      const label = options.accessRequired && leg.mode === 'walk'
        ? 'outdoor transfer (step-free route unverified); standard pedestrian estimate'
        : modeLabel(leg.mode).toLowerCase()
      lines.push(`   then ${label} about ${leg.minutes} min${fare}`)
    }
  })

  lines.push('')
  const budget = t.budget !== null && t.budget !== undefined ? ` of ${fmtMoney(t.budget, t.currency)} budget` : ''
  lines.push(`Estimated upper total per person: ${fmtMoney(t.worst_case, t.currency)}${budget} (includes 10% contingency)`)
  const route = routeUrlFor(plan)
  if (route) lines.push(`Route: ${route}`)
  const share = safeHttpsUrl(options.shareUrl) ?? (typeof options.shareUrl === 'string' && /^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?\/p\//.test(options.shareUrl) ? options.shareUrl : null)
  if (share) lines.push(`Plan: ${share}`)
  return lines.join('\n')
}

export function whatsappUrl(text: string): string {
  return `https://wa.me/?text=${encodeURIComponent(text)}`
}

/** Copy text to the clipboard, falling back to a hidden textarea. Resolves true on success. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to the legacy path
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}
