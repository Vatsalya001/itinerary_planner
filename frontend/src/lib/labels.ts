import { humanize } from './format'

const CONSTRAINT_NAMES: Record<string, string> = {
  budget: 'Budget',
  time: 'Available time',
  'diet:vegetarian': 'Vegetarian',
  'diet:vegan': 'Vegan',
  'diet:jain': 'Jain',
  'alcohol:avoid': 'No alcohol venues',
  'access:wheelchair': 'Wheelchair access',
  'crowd:avoid': 'Avoid crowded places',
  'setting:indoors_only': 'Indoors only',
  'setting:outdoors_only': 'Outdoors only',
}

export function constraintName(key: string): string {
  if (CONSTRAINT_NAMES[key]) return CONSTRAINT_NAMES[key]
  if (key.startsWith('pacing:')) return `Pacing requirement: ${humanize(key.split(':')[1]).toLowerCase()}`
  const [kind, value] = key.split(':', 2)
  if (value && kind === 'must_include') return `Must include: ${humanize(value).toLowerCase()}`
  if (value && kind === 'must_avoid') return `Avoid: ${humanize(value).toLowerCase()}`
  return humanize(value ?? key)
}

const ID_LIKE = /^(gp|snap|fx|ev|evfx):/

/** What was left out: the server-filled name, else a free-text idea, else a neutral label (never a raw ID). */
export function omissionLabel(o: { ref: string; name?: string | null }): string {
  const name = o.name?.trim()
  if (name) return name
  return ID_LIKE.test(o.ref) || !o.ref.trim() ? 'Another option' : o.ref
}

export type DegradationKind = 'data' | 'planner' | 'parser' | 'model' | 'checked'

/**
 * Sort a degradation message into what actually happened: missing data (weather, places, events) versus an AI
 * step that was replaced by code. Uses the emitting node when known, else the backend's message wording.
 */
export function classifyDegradation(text: string, node?: string | null): DegradationKind {
  const t = text.toLowerCase()
  if (/used the plan it had already checked/.test(t)) return 'checked'
  if (/rule-based planner|deterministic planner/.test(t)) return 'planner'
  if (/switched to/.test(t) && /model/.test(t)) return 'model' 
  if (/\bpars(er|ing)\b/.test(t)) return 'parser'
  if (node === 'plan_agent' || /deterministic planner|rule-based planner|ai planner|ai model/.test(t)) return 'planner'
  return 'data'
}

export const DEGRADATION_TITLES: Record<DegradationKind, string> = {
  checked: 'Used the checked AI plan',
  data: 'Running with reduced data',
  planner: 'The rule-based planner took over',
  parser: 'Read with the rule-based parser',
  model: 'Switched to the backup AI model',
}

export const DEGRADATION_NOTES: Record<DegradationKind, string | null> = {
  checked: null,
  data: null,
  planner: 'The AI planner stopped, so a simpler rule-based planner built the plan. It goes through the same checks.',
  parser: 'The AI couldn’t read your request, so rule-based parsing was used. Check “What I understood”.',
  model: null,
}
