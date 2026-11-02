import type { RunPhase } from '../hooks/usePlanRun'
import type { Status } from '../types'
import { describeError } from './describeError'

const DONE: Record<Status, string> = {
  complete: 'Plan ready.',
  fallback: 'Backup plan ready, built by the rule-based planner.',
  needs_clarification: 'Waiting for your answer.',
  no_feasible_plan: 'No plan fits every constraint.',
  rejected: 'Request not accepted.',
}

/** Milestones announced to screen readers: started, waiting for your answer, done. Never individual steps. */
export function runStatusText(phase: RunPhase, resumed: boolean, hadSteps: boolean): string {
  switch (phase.kind) {
    case 'running':
      return resumed ? 'Planning resumed with your answer.' : 'Planning started.'
    case 'done':
      return DONE[phase.result.status] ?? 'Planning finished.'
    case 'error':
      return `Planning stopped: ${describeError(phase.error).title}.`
    default:
      return hadSteps ? 'Planning stopped.' : ''
  }
}
