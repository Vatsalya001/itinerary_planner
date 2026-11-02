import type { Status, TraceEvent } from '../types'
import { humanize } from './format'
import { classifyDegradation, type DegradationKind } from './labels'

export type StepKind = 'run' | 'node' | 'tool' | 'validation' | 'degraded' | 'clarification' | 'result' | 'error'
export type StepStatus = 'running' | 'done' | 'failed' | 'warn' | 'info'

export interface TraceStep {
  key: string
  kind: StepKind
  /** Raw node or tool name from the backend, e.g. "search_places". */
  name: string | null
  /** The graph node that emitted the event ("tools" for the model's own tool calls). */
  node: string | null
  /** Plain-language label. */
  label: string
  status: StepStatus
  /** The model's one-line reason; only set for the model's own tool calls (node "tools"). */
  reason: string | null
  summary: string | null
  durationMs: number | null
  startedTs: string | null
  details: string[]
}

export interface Degradation {
  text: string
  kind: DegradationKind
}

export interface TraceState {
  steps: TraceStep[]
  degraded: Degradation[]
  seen: Record<string, true>
  lastEventAt: string | null
  finished: boolean
  /** Last agent turn number shown, so a turn is never labelled twice. */
  lastAgentTurn: number | null
}

export const initialTrace: TraceState = {
  steps: [],
  degraded: [],
  seen: {},
  lastEventAt: null,
  finished: false,
  lastAgentTurn: null,
}

const NODE_LABELS: Record<string, string> = {
  guard_input: 'Checked the request is complete and in range',
  parse_preferences: 'Read what you asked for',
  check_clarity: 'Checked whether anything was unclear',
  ask_clarification: 'Asked you a quick question',
  resolve_location: 'Found the city',
  build_context: 'Set up the day: weather, time window and pacing',
  get_weather: "Checked Saturday's weather",
  gather_candidates: 'Gathered places and events',
  plan_agent: 'Agent chose the next step',
  validate_plan: 'Checked budget, time, opening hours, diet and more',
  fallback_planner: 'Built a backup plan without the AI model',
  explain_infeasible: 'Worked out what blocked a plan',
  finalize: 'Saved the plan',
}

const NODE_RUNNING_LABELS: Record<string, string> = {
  guard_input: 'Checking the request',
  parse_preferences: 'Reading what you asked for',
  check_clarity: 'Checking whether anything is unclear',
  ask_clarification: 'Preparing a quick question',
  resolve_location: 'Finding the city',
  build_context: 'Setting up the day: weather, time window and pacing',
  get_weather: "Checking Saturday's weather",
  gather_candidates: 'Gathering places and events',
  plan_agent: 'Agent is choosing the next step',
  validate_plan: 'Checking budget, time, opening hours, diet and more',
  fallback_planner: 'Building a backup plan without the AI model',
  explain_infeasible: 'Working out what blocked a plan',
  finalize: 'Saving the plan',
}

const TOOL_LABELS: Record<string, string> = {
  get_weather: "Checked Saturday's weather",
  resolve_location: 'Looked up the city',
  search_places: 'Searched for places',
  search_events: 'Looked for events on the day',
  estimate_travel: 'Estimated travel between stops',
  check_plan: 'Test-checked a draft plan',
  submit_plan: 'Submitted the plan for checking',
}

const TOOL_RUNNING_LABELS: Record<string, string> = {
  get_weather: "Checking Saturday's weather",
  resolve_location: 'Looking up the city',
  search_places: 'Searching for places',
  search_events: 'Looking for events on the day',
  estimate_travel: 'Estimating travel between stops',
  check_plan: 'Test-checking a draft plan',
  submit_plan: 'Submitting the plan for checking',
}

const TOOL_FAILED_LABELS: Record<string, string> = {
  get_weather: 'Weather check failed',
  resolve_location: 'City lookup failed',
  search_places: 'Place search failed',
  search_events: 'Event search failed',
  estimate_travel: 'Travel estimate failed',
  check_plan: 'Draft check failed',
  submit_plan: 'Plan submission failed',
}

export function toolFailedLabel(tool: string): string {
  return TOOL_FAILED_LABELS[tool] ?? `${humanize(tool)} failed`
}

const RESULT_LABELS: Record<Status, string> = {
  complete: 'Plan ready',
  fallback: 'Backup plan ready',
  needs_clarification: 'Waiting for your answer',
  no_feasible_plan: 'No plan fits every constraint',
  rejected: 'Request not accepted',
}

/** Nodes whose work is already shown by more specific events. */
const HIDDEN_NODES = new Set(['tools'])

export function nodeLabel(node: string, running = false): string {
  return (running ? NODE_RUNNING_LABELS[node] : NODE_LABELS[node]) ?? humanize(node)
}

export function toolLabel(tool: string, running = false): string {
  return (running ? TOOL_RUNNING_LABELS[tool] : TOOL_LABELS[tool]) ?? humanize(tool)
}

function durationBetween(startTs: string | null, endTs: string): number | null {
  if (!startTs || !endTs) return null
  const a = Date.parse(startTs)
  const b = Date.parse(endTs)
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return null
  return b - a
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null
}

function violationMessages(data: Record<string, unknown> | null | undefined): { messages: string[]; errors: number } {
  const list = data && Array.isArray(data.violations) ? data.violations : []
  const messages: string[] = []
  let errors = 0
  for (const v of list) {
    if (v && typeof v === 'object') {
      const o = v as Record<string, unknown>
      if (o.severity === 'error') errors += 1
      const msg = str(o.message) ?? str(o.code)
      if (msg) messages.push(o.severity === 'warning' ? `Warning: ${msg}` : msg)
    }
  }
  return { messages, errors }
}

/** The model's reason, only for the model's own tool calls; code-run steps never get an "Agent's note". */
function agentReason(ev: TraceEvent): string | null {
  return ev.node === 'tools' ? str(ev.reason) : null
}

function newStep(ev: TraceEvent, partial: Partial<TraceStep> & Pick<TraceStep, 'kind' | 'label' | 'status'>): TraceStep {
  return {
    key: `${ev.run_id}:${ev.seq}:${ev.type}`,
    name: null,
    node: ev.node ?? null,
    reason: agentReason(ev),
    // A code-side reason (older backends) is still useful context, shown as a plain summary.
    summary: str(ev.summary) ?? (ev.node !== 'tools' ? str(ev.reason) : null),
    durationMs: typeof ev.duration_ms === 'number' ? ev.duration_ms : null,
    startedTs: ev.ts || null,
    details: [],
    ...partial,
  }
}

/** Index of the most recent running step of `kind` with `name`. */
function findRunning(steps: TraceStep[], kind: StepKind, name: string | null): number {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const s = steps[i]
    if (s.kind === kind && s.status === 'running' && s.name === name) return i
  }
  return -1
}

function completeStep(
  state: TraceState,
  ev: TraceEvent,
  kind: 'node' | 'tool',
  name: string,
  status: StepStatus,
): TraceStep[] {
  const steps = state.steps.slice()
  const idx = findRunning(steps, kind, name)
  const label = kind === 'node' ? nodeLabel(name) : toolLabel(name)
  const failedLabel = kind === 'tool' ? toolFailedLabel(name) : label
  if (idx === -1) {
    steps.push(newStep(ev, { kind, name, label: status === 'failed' ? failedLabel : label, status }))
    return steps
  }
  const prev = steps[idx]
  steps[idx] = {
    ...prev,
    label: status === 'failed' ? failedLabel : label,
    status,
    reason: prev.reason ?? agentReason(ev),
    summary: str(ev.summary) ?? prev.summary,
    durationMs: typeof ev.duration_ms === 'number' ? ev.duration_ms : durationBetween(prev.startedTs, ev.ts),
  }
  return steps
}

const TURN_PREFIX = /^turn\s+\d+\s*:\s*/i

/**
 * Finish a plan_agent step. The backend summary starts with "turn N: ..."; the number moves into the label, and a
 * turn already shown (e.g. "turn 4: check_plan" then "turn 4: stopped (...)") is never numbered twice.
 */
function completeAgentTurn(state: TraceState, ev: TraceEvent): TraceState {
  const steps = completeStep(state, ev, 'node', 'plan_agent', 'done')
  const turn = typeof ev.data?.turn === 'number' ? ev.data.turn : null
  const raw = str(ev.summary)
  let summary = raw ? raw.replace(TURN_PREFIX, '') : null
  const stopped = summary ? /^stopped\s*\((.*)\)\s*$/i.exec(summary) : null
  if (stopped) summary = stopped[1] || null
  const fresh = turn !== null && turn !== state.lastAgentTurn
  const label = stopped ? 'Agent stopped' : fresh ? `Agent turn ${turn}` : nodeLabel('plan_agent')
  // completeStep updated (or appended) the matching step; it is the last plan_agent node step.
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    if (steps[i].kind === 'node' && steps[i].name === 'plan_agent') {
      steps[i] = { ...steps[i], label, summary, status: stopped ? 'warn' : steps[i].status }
      break
    }
  }
  return { ...state, steps, lastAgentTurn: turn ?? state.lastAgentTurn }
}

/** Pure reducer from streamed TraceEvents to the steps shown in "How I planned this". */
export function reduceTrace(state: TraceState, ev: TraceEvent): TraceState {
  const seenKey = `${ev.run_id}:${ev.seq}:${ev.type}`
  if (ev.type !== 'heartbeat' && ev.seq > 0 && state.seen[seenKey]) return state // duplicate delivery
  const seen = ev.type === 'heartbeat' ? state.seen : { ...state.seen, [seenKey]: true as const }
  const base: TraceState = { ...state, seen, lastEventAt: ev.ts || state.lastEventAt }

  switch (ev.type) {
    case 'heartbeat':
      return base
    case 'run_started':
      return {
        ...base,
        finished: false,
        steps: [
          ...state.steps,
          newStep(ev, {
            kind: 'run',
            label: state.steps.length ? 'Picked up again with your answer' : 'Started planning',
            status: 'info',
          }),
        ],
      }
    case 'node_started': {
      const node = ev.node ?? 'step'
      if (HIDDEN_NODES.has(node)) return base
      return {
        ...base,
        steps: [...state.steps, newStep(ev, { kind: 'node', name: node, label: nodeLabel(node, true), status: 'running' })],
      }
    }
    case 'node_completed': {
      const node = ev.node ?? 'step'
      if (HIDDEN_NODES.has(node)) return base
      if (node === 'plan_agent') return completeAgentTurn(base, ev)
      return { ...base, steps: completeStep(state, ev, 'node', node, 'done') }
    }
    case 'tool_called': {
      const tool = ev.tool ?? 'tool'
      return {
        ...base,
        steps: [...state.steps, newStep(ev, { kind: 'tool', name: tool, label: toolLabel(tool, true), status: 'running' })],
      }
    }
    case 'tool_completed':
      return { ...base, steps: completeStep(state, ev, 'tool', ev.tool ?? 'tool', 'done') }
    case 'tool_failed':
      return { ...base, steps: completeStep(state, ev, 'tool', ev.tool ?? 'tool', 'failed') }
    case 'validation': {
      const { messages, errors } = violationMessages(ev.data)
      const okFlag = ev.data && typeof ev.data.ok === 'boolean' ? ev.data.ok : errors === 0
      return {
        ...base,
        steps: [
          ...state.steps,
          newStep(ev, {
            kind: 'validation',
            name: ev.node ?? null,
            label: okFlag ? 'Plan passed the checks' : 'Checks found problems to repair',
            status: okFlag ? 'done' : 'warn',
            details: messages,
          }),
        ],
      }
    }
    case 'degraded': {
      const text = str(ev.summary) ?? str(ev.data?.reason) ?? str(ev.data?.message) ?? 'Something was unavailable'
      const kind = classifyDegradation(text, ev.node)
      return {
        ...base,
        degraded: state.degraded.some((d) => d.text === text) ? state.degraded : [...state.degraded, { text, kind }],
        steps: [...state.steps, newStep(ev, { kind: 'degraded', label: text, status: 'warn', summary: null })],
      }
    }
    case 'clarification':
      return {
        ...base,
        steps: [...state.steps, newStep(ev, { kind: 'clarification', label: 'Asked you a quick question', status: 'info' })],
      }
    case 'result': {
      const status = ev.data && typeof ev.data.status === 'string' ? (ev.data.status as Status) : null
      return {
        ...base,
        finished: true,
        steps: [
          ...state.steps,
          newStep(ev, {
            kind: 'result',
            label: (status && RESULT_LABELS[status]) || 'Finished',
            status: status === 'complete' || status === 'fallback' ? 'done' : 'info',
            summary: str(ev.summary),
          }),
        ],
      }
    }
    case 'error':
      return {
        ...base,
        finished: true,
        steps: [
          ...state.steps,
          newStep(ev, {
            kind: 'error',
            label: 'The planner hit an error',
            status: 'failed',
            summary: str(ev.summary) ?? str(ev.data?.message),
          }),
        ],
      }
    default:
      return base
  }
}

export type TraceAction = { type: 'event'; event: TraceEvent } | { type: 'reset' }

export function traceReducer(state: TraceState, action: TraceAction): TraceState {
  return action.type === 'reset' ? initialTrace : reduceTrace(state, action.event)
}
