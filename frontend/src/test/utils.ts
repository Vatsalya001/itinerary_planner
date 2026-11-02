import type { PlanResult } from '../types'
import clarification from './fixtures/plan_clarification.json'
import complete from './fixtures/plan_complete.json'
import fallback from './fixtures/plan_fallback.json'
import infeasible from './fixtures/plan_infeasible.json'
import rejected from './fixtures/plan_rejected.json'

/** Fresh deep copies of the fixtures, typed as the contract. */
export const fixtures = {
  complete: () => structuredClone(complete) as unknown as PlanResult,
  fallback: () => structuredClone(fallback) as unknown as PlanResult,
  clarification: () => structuredClone(clarification) as unknown as PlanResult,
  infeasible: () => structuredClone(infeasible) as unknown as PlanResult,
  rejected: () => structuredClone(rejected) as unknown as PlanResult,
}
