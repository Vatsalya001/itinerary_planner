/**
 * TypeScript mirror of backend/app/domain/schemas.py (the JSON contract).
 *
 * Conventions (same as the backend):
 * - Local times are integer minutes since local midnight on the target date; values above 1440 mean
 *   "after midnight" (25:30 -> 1530). Durations are integer minutes.
 * - Money is whole currency units per person (ints). `currency` is ISO 4217.
 * - Dates are ISO strings ("2026-11-07"); datetimes are ISO strings with offset.
 */

export type Energy = 'low' | 'medium' | 'high'
export type Evidence = 'confirmed' | 'likely' | 'unknown' | 'no'
export type Effort = 'low' | 'medium' | 'high'
export type Status = 'complete' | 'fallback' | 'needs_clarification' | 'no_feasible_plan' | 'rejected'
export type Mode = 'agent' | 'deterministic'
export type CostBasis =
  | 'listed_price_range'
  | 'price_level_estimate'
  | 'ticket_price'
  | 'free'
  | 'category_estimate'
export type ProviderName = 'google_places' | 'curated_snapshot' | 'fixture' | 'web_event' | 'fixture_event'
export type TravelMode = 'walk' | 'auto' | 'cab' | 'metro'
export type ConstraintKind = 'diet' | 'alcohol' | 'access' | 'crowd' | 'setting' | 'must_include' | 'must_avoid'

// ---------------------------------------------------------------------------
// Request and preferences
// ---------------------------------------------------------------------------

export interface PlanRequest {
  free_text?: string | null
  city?: string | null
  budget?: number | null
  currency?: string | null
  available_time?: string | number | null
  start_time?: string | null
  date?: string | null
  mood?: string | null
  interests?: string[]
  constraints?: string[]
  neighbourhood?: string | null
  group_size?: number
  /** Constraint keys dropped via a one-tap relaxation, e.g. ["access:wheelchair"]; never re-derived from text. */
  relaxed_constraints?: string[]
}

/** Every key PlanRequest accepts (the backend forbids extra keys). */
export const PLAN_REQUEST_KEYS: ReadonlyArray<keyof PlanRequest> = [
  'free_text',
  'city',
  'budget',
  'currency',
  'available_time',
  'start_time',
  'date',
  'mood',
  'interests',
  'constraints',
  'neighbourhood',
  'group_size',
  'relaxed_constraints',
]

export interface Constraint {
  kind: ConstraintKind
  value: string
  source_text?: string
}

export interface Preferences {
  city: string | null
  budget: number | null
  currency: string | null
  start_min: number
  available_minutes: number
  date: string | null
  mood: string
  energy: Energy
  interests: string[]
  hard_constraints: Constraint[]
  soft_preferences: string[]
  neighbourhood: string | null
  group_size: number
  assumptions: string[]
}

export interface ClarificationQuestion {
  id: string
  question: string
  options: string[]
  field?: string | null
}

// ---------------------------------------------------------------------------
// World context
// ---------------------------------------------------------------------------

export interface Weather {
  status: 'ok' | 'unavailable'
  summary: string
  precip_prob_max?: number | null
  temp_max_c?: number | null
  temp_min_c?: number | null
  weather_code?: number | null
  sunrise_min?: number | null
  sunset_min?: number | null
}

// ---------------------------------------------------------------------------
// Candidates
// ---------------------------------------------------------------------------

export interface CostRange {
  min: number
  max: number
  currency: string
  basis: CostBasis
}

export interface Hours {
  status: 'known' | 'unknown'
  periods: Array<[number, number]>
}

export interface Source {
  provider: ProviderName
  url?: string | null
  retrieved_at?: string | null
  last_checked?: string | null
  attribution?: string | null
}

export interface Candidate {
  id: string
  kind: 'place' | 'event'
  name: string
  category: string
  city_id: string
  lat: number
  lng: number
  address?: string | null
  neighbourhood?: string | null
  cost: CostRange
  rating?: number | null
  rating_count?: number | null
  hours: Hours
  diet: Record<string, Evidence>
  wheelchair: Evidence
  indoor?: boolean | null
  seated: boolean
  effort: Effort
  serves_alcohol?: boolean | null
  live_music?: boolean | null
  popularity: 'low' | 'medium' | 'high'
  busy_windows: Array<[number, number]>
  tags: string[]
  duration_range: [number, number, number]
  event_start_min?: number | null
  event_end_min?: number | null
  event_date?: string | null
  maps_url?: string | null
  source: Source
}

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

export interface Omission {
  ref: string
  reason: string
  /** Display name filled server-side from the run's ledger; null for ideas or unknown IDs. */
  name?: string | null
}

export interface Leg {
  from_id: string
  to_id: string
  mode: TravelMode
  distance_km: number
  minutes: number
  minutes_max: number
  fare: CostRange
  basis: 'estimate'
}

export interface ScheduledStop {
  candidate: Candidate
  start_min: number
  end_min: number
  duration_min: number
  why: string
  tradeoff?: string | null
  plan_b?: Candidate | null
  warnings: string[]
}

export interface Totals {
  currency: string
  stops_cost_min: number
  stops_cost_max: number
  legs_cost_min: number
  legs_cost_max: number
  contingency: number
  worst_case: number
  budget: number | null
  budget_left: number | null
  elapsed_min: number
  available_minutes: number
  minutes_left: number
  walking_min: number
}

export interface CanonicalPlan {
  date: string
  city: string
  timezone: string
  start_min: number
  end_min: number
  stops: ScheduledStop[]
  legs: Leg[]
  totals: Totals
  summary_note: string
  omitted: Omission[]
  weather: Weather
  maps_route_url?: string | null
}

export interface Violation {
  code: string
  severity: 'error' | 'warning'
  message: string
  stop_index?: number | null
  candidate_id?: string | null
}

export interface CheckItem {
  name: string
  status: 'pass' | 'warn' | 'unverified' | 'fail'
  detail: string
}

export interface Relaxation {
  label: string
  patch: Record<string, unknown>
}

export interface InfeasibleReport {
  reason: string
  binding_constraints: string[]
  relaxations: Relaxation[]
  cheapest_budget?: number | null
  min_minutes?: number | null
}

export interface PlanResult {
  run_id: string
  status: Status
  mode?: Mode | null
  model?: string | null
  plan?: CanonicalPlan | null
  checks: CheckItem[]
  questions: ClarificationQuestion[]
  thread_id?: string | null
  preferences?: Preferences | null
  violations: Violation[]
  infeasible?: InfeasibleReport | null
  degradations: string[]
  assumptions: string[]
  warnings: string[]
  message?: string | null
  share_id?: string | null
  latency_ms?: number | null
}

// ---------------------------------------------------------------------------
// Trace events
// ---------------------------------------------------------------------------

export type TraceType =
  | 'run_started'
  | 'node_started'
  | 'node_completed'
  | 'tool_called'
  | 'tool_completed'
  | 'tool_failed'
  | 'validation'
  | 'degraded'
  | 'clarification'
  | 'result'
  | 'error'
  | 'heartbeat'

export interface TraceEvent {
  type: TraceType
  run_id: string
  seq: number
  ts: string
  node?: string | null
  tool?: string | null
  reason?: string | null
  duration_ms?: number | null
  summary?: string | null
  data?: Record<string, unknown> | null
}

// ---------------------------------------------------------------------------
// Other endpoints
// ---------------------------------------------------------------------------

export type FeedbackRating = 'up' | 'down'

export interface EvalRow {
  model?: string | null
  kind?: 'baseline' | 'api' | 'local' | string | null
  runs?: number | null
  hard_pass_rate?: number | null
  status_accuracy?: number | null
  agent_success?: number | null
  first_submission_validity?: number | null
  tool_protocol_errors?: number | null
  clarification_precision?: number | null
  clarification_recall?: number | null
  judge_quality?: number | null
  latency_p50_ms?: number | null
  latency_p95_ms?: number | null
  cost_per_plan_usd?: number | null
}

export interface EvalSummary {
  generated_at?: string | null
  git_sha?: string | null
  rows?: EvalRow[] | null
  categories?: Record<string, unknown> | null
}
