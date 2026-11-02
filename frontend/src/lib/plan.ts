import type { CanonicalPlan, Leg, PlanRequest, ProviderName } from '../types'
import { PLAN_REQUEST_KEYS } from '../types'

/** The leg from stop `index` to stop `index + 1`, matched by IDs with a positional fallback. */
export function legAfter(plan: CanonicalPlan, index: number): Leg | null {
  const from = plan.stops[index]?.candidate.id
  const to = plan.stops[index + 1]?.candidate.id
  if (!from || !to) return null
  return plan.legs.find((l) => l.from_id === from && l.to_id === to) ?? plan.legs[index] ?? null
}

/** Providers used anywhere in the plan (stops and Plan Bs), for attribution. */
export function providersIn(plan: CanonicalPlan): Set<ProviderName> {
  const out = new Set<ProviderName>()
  for (const s of plan.stops) {
    out.add(s.candidate.source.provider)
    if (s.plan_b) out.add(s.plan_b.source.provider)
  }
  return out
}

/**
 * Apply a relaxation patch (PlanRequest fields) to a request. Unknown keys are dropped because the API
 * forbids extra fields; null removes a field.
 */
export function applyPatch(request: PlanRequest, patch: Record<string, unknown>): PlanRequest {
  const next: Record<string, unknown> = { ...request }
  for (const [key, value] of Object.entries(patch)) {
    if (!(PLAN_REQUEST_KEYS as ReadonlyArray<string>).includes(key)) continue
    if (value === null || value === undefined) delete next[key]
    else next[key] = value
  }
  return next as PlanRequest
}
