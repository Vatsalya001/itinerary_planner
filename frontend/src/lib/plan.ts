import type { CanonicalPlan, Leg, PlanRequest, ProviderName } from '../types'
import { PLAN_REQUEST_KEYS } from '../types'

/**
 * The leg from stop `index` to stop `index + 1`, matched by `from_id`/`to_id` only. There is no positional
 * fallback: a leg that does not join these two stops is never shown (or copied) between them.
 */
export function legAfter(plan: CanonicalPlan, index: number): Leg | null {
  const from = plan.stops[index]?.candidate.id
  const to = plan.stops[index + 1]?.candidate.id
  if (!from || !to) return null
  return plan.legs.find((l) => l.from_id === from && l.to_id === to) ?? null
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

/** Keys whose patch values are unioned with the request's existing list instead of replacing it. */
const UNION_KEYS = new Set<string>(['relaxed_constraints'])

/** Ordered union of string lists without duplicates. */
export function unionStrings(...lists: Array<readonly unknown[] | null | undefined>): string[] {
  const out: string[] = []
  for (const list of lists) {
    for (const item of list ?? []) {
      if (typeof item === 'string' && item && !out.includes(item)) out.push(item)
    }
  }
  return out
}

/**
 * Apply a relaxation patch (PlanRequest fields) to a request. Unknown keys are dropped because the API
 * forbids extra fields; null removes a field. `relaxed_constraints` accumulates: dropping a second constraint
 * keeps the first one dropped. Every other key replaces the previous value.
 */
export function applyPatch(request: PlanRequest, patch: Record<string, unknown>): PlanRequest {
  const next: Record<string, unknown> = { ...request }
  for (const [key, value] of Object.entries(patch)) {
    if (!(PLAN_REQUEST_KEYS as ReadonlyArray<string>).includes(key)) continue
    if (value === null || value === undefined) delete next[key]
    else if (UNION_KEYS.has(key) && Array.isArray(value)) {
      const merged = unionStrings(next[key] as unknown[] | undefined, value)
      if (merged.length) next[key] = merged
      else delete next[key]
    } else next[key] = value
  }
  return next as PlanRequest
}
