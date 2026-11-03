import type { PlanRequest } from '../types'
import { fmtAmount, fmtMoney } from './format'
import { unionStrings } from './plan'

/** Constraint toggles; each maps to text the backend's constraint vocabulary understands. */
export const CONSTRAINT_TOGGLES = [
  { id: 'vegetarian', label: 'Vegetarian', text: 'vegetarian', key: 'diet:vegetarian' },
  { id: 'vegan', label: 'Vegan', text: 'vegan', key: 'diet:vegan' },
  { id: 'jain', label: 'Jain', text: 'jain', key: 'diet:jain' },
  { id: 'no_alcohol', label: 'No alcohol venues', text: 'no alcohol', key: 'alcohol:avoid' },
  { id: 'wheelchair', label: 'Wheelchair accessible', text: 'wheelchair accessible', key: 'access:wheelchair' },
  { id: 'avoid_crowds', label: 'Avoid crowded places', text: 'avoid crowded places', key: 'crowd:avoid' },
  { id: 'indoors_only', label: 'Indoors only', text: 'indoors only', key: 'setting:indoors_only' },
] as const

export type ToggleId = (typeof CONSTRAINT_TOGGLES)[number]['id']

export const INTEREST_PRESETS = [
  'food',
  'music',
  'walks',
  'coffee',
  'art',
  'museums',
  'books',
  'parks',
  'events',
  'dessert',
  'shopping',
  'games',
]

export const CURRENCIES = ['INR', 'USD', 'GBP', 'EUR', 'SGD', 'AED', 'JPY', 'AUD', 'CAD']

export const DURATION_OPTIONS: Array<{ minutes: number; label: string }> = [
  { minutes: 60, label: '1 hour' },
  { minutes: 90, label: '1.5 hours' },
  { minutes: 120, label: '2 hours' },
  { minutes: 150, label: '2.5 hours' },
  { minutes: 180, label: '3 hours' },
  { minutes: 240, label: '4 hours' },
  { minutes: 300, label: '5 hours' },
  { minutes: 360, label: '6 hours' },
  { minutes: 480, label: '8 hours' },
  { minutes: 600, label: '10 hours' },
  { minutes: 720, label: '12 hours' },
]

export const LIMITS = {
  freeText: 1000,
  city: 80,
  mood: 300,
  neighbourhood: 80,
  interest: 40,
  constraint: 120,
  maxItems: 10,
  budgetMax: 1_000_000,
  groupMin: 1,
  groupMax: 20,
} as const

export type FieldKey =
  | 'city'
  | 'budget'
  | 'currency'
  | 'startTime'
  | 'available'
  | 'mood'
  | 'interests'
  | 'constraints'
  | 'neighbourhood'
  | 'groupSize'

export interface FormState {
  freeText: string
  city: string
  budget: string
  currency: string // '' = from the city
  startTime: string // "HH:MM"
  availableMinutes: string
  mood: string
  interests: string[]
  toggles: Record<ToggleId, boolean>
  extraConstraints: string
  neighbourhood: string
  groupSize: string
  /** Constraint keys dropped via "no plan fits" relaxations; sent as relaxed_constraints until brought back. */
  relaxed: string[]
}

export type DirtyFields = Partial<Record<FieldKey, boolean>>

/** The brief's example as a sentence. */
export const DEFAULT_FREE_TEXT =
  "I'm in Bangalore with ₹2000 and 4 hours this Saturday. " +
  "I like food, music and walks. I'm vegetarian and want to avoid crowded places."

const NO_TOGGLES: Record<ToggleId, boolean> = {
  vegetarian: false,
  vegan: false,
  jain: false,
  no_alcohol: false,
  wheelchair: false,
  avoid_crowds: false,
  indoors_only: false,
}

/** The brief's JSON, as form state. */
export function defaultFormState(): FormState {
  return {
    freeText: DEFAULT_FREE_TEXT,
    city: 'Bangalore',
    budget: '2000',
    currency: '',
    startTime: '15:00',
    availableMinutes: '240',
    mood: '',
    interests: ['food', 'music', 'walks'],
    toggles: { ...NO_TOGGLES, vegetarian: true, avoid_crowds: true },
    extraConstraints: '',
    neighbourhood: '',
    groupSize: '1',
    relaxed: [],
  }
}

/**
 * The budget range error, grouped like fmtMoney for the chosen currency: "₹0 and ₹10,00,000" for INR,
 * "$0 and $1,000,000" for USD. With "From city" the currency is unknown, so plain western grouping is used.
 */
export function budgetRangeMessage(currency: string): string {
  const code = currency.trim()
  const bound = (n: number) => (code ? fmtMoney(n, code) : fmtAmount(n, null))
  return `Enter a budget between ${bound(0)} and ${bound(LIMITS.budgetMax)}.`
}

export function splitList(text: string): string[] {
  return text
    .split(/[,\n;]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

export function formConstraints(form: FormState): string[] {
  const fromToggles = CONSTRAINT_TOGGLES.filter((t) => form.toggles[t.id]).map((t) => t.text)
  const extra = splitList(form.extraConstraints)
  const all: string[] = []
  for (const c of [...fromToggles, ...extra]) {
    if (!all.some((x) => x.toLowerCase() === c.toLowerCase())) all.push(c)
  }
  return all
}

/** Client-side checks mirroring the API bounds. Returns user-facing messages keyed by field. */
export function validateForm(form: FormState): Partial<Record<FieldKey | 'freeText' | 'form', string>> {
  const errors: Partial<Record<FieldKey | 'freeText' | 'form', string>> = {}
  if (form.freeText.length > LIMITS.freeText) errors.freeText = `Please keep this under ${LIMITS.freeText} characters.`
  if (form.budget.trim() !== '') {
    const b = Number(form.budget)
    if (!Number.isFinite(b) || b < 0 || b > LIMITS.budgetMax) errors.budget = budgetRangeMessage(form.currency)
  }
  const g = Number(form.groupSize)
  if (!Number.isInteger(g) || g < LIMITS.groupMin || g > LIMITS.groupMax) errors.groupSize = 'Group size is 1 to 20.'
  if (formConstraints(form).length > LIMITS.maxItems) errors.constraints = `At most ${LIMITS.maxItems} constraints.`
  if (formConstraints(form).some((c) => c.length > LIMITS.constraint))
    errors.constraints = `Each constraint must be at most ${LIMITS.constraint} characters.`
  if (form.interests.length > LIMITS.maxItems) errors.interests = `Pick at most ${LIMITS.maxItems} interests.`
  if (!form.freeText.trim() && !form.city.trim()) errors.form = 'Describe your Saturday or fill in at least the city.'
  return errors
}

/**
 * Build the PlanRequest. With a description, it is sent as free text and only the form fields you edited
 * are added (they override what the planner reads from the text). With an empty description, every form
 * field is sent, which with the defaults is exactly the brief's JSON.
 */
export function buildRequest(form: FormState, dirty: DirtyFields): PlanRequest {
  const req: PlanRequest = {}
  const text = form.freeText.trim()
  const structured = text === ''
  const include = (k: FieldKey) => structured || Boolean(dirty[k])

  if (text) req.free_text = text
  if (include('city') && form.city.trim()) req.city = form.city.trim()
  if (include('budget') && form.budget.trim() !== '' && Number.isFinite(Number(form.budget)))
    req.budget = Number(form.budget)
  if (include('currency') && form.currency) req.currency = form.currency
  if (dirty.startTime && form.startTime) req.start_time = form.startTime
  if (include('available') && form.availableMinutes) req.available_time = Number(form.availableMinutes)
  if (include('mood') && form.mood.trim()) req.mood = form.mood.trim()
  if (include('interests') && form.interests.length) req.interests = form.interests.slice(0, LIMITS.maxItems)
  if (include('constraints')) {
    const c = formConstraints(form)
    if (c.length) req.constraints = c.slice(0, LIMITS.maxItems)
  }
  if (include('neighbourhood') && form.neighbourhood.trim()) req.neighbourhood = form.neighbourhood.trim()
  const group = Number(form.groupSize)
  if (Number.isInteger(group) && (dirty.groupSize || (structured && group !== 1))) req.group_size = group
  if (form.relaxed.length) req.relaxed_constraints = form.relaxed.slice(0, LIMITS.maxItems)
  return req
}

/** Toggle a constraint; switching one on also brings it back if it had been dropped. */
export function toggleConstraint(form: FormState, id: ToggleId): FormState {
  const on = !form.toggles[id]
  const key = CONSTRAINT_TOGGLES.find((t) => t.id === id)?.key
  return {
    ...form,
    toggles: { ...form.toggles, [id]: on },
    relaxed: key ? (on ? form.relaxed.filter((k) => k !== key) : [...new Set([...form.relaxed, key])]) : form.relaxed,
  }
}

/** Undo a dropped constraint. */
export function restoreConstraint(form: FormState, key: string): FormState {
  const toggle = CONSTRAINT_TOGGLES.find((t) => t.key === key)
  return {
    ...form,
    relaxed: form.relaxed.filter((k) => k !== key),
    toggles: toggle ? { ...form.toggles, [toggle.id]: true } : form.toggles,
  }
}

/** Reflect a relaxation patch in the form so what you see matches what was sent. */
export function patchForm(
  form: FormState,
  dirty: DirtyFields,
  patch: Record<string, unknown>,
): { form: FormState; dirty: DirtyFields } {
  const next: FormState = { ...form, toggles: { ...form.toggles } }
  const d: DirtyFields = { ...dirty }
  for (const [key, value] of Object.entries(patch)) {
    switch (key) {
      case 'budget':
        if (typeof value === 'number') {
          next.budget = String(value)
          d.budget = true
        }
        break
      case 'available_time':
        if (typeof value === 'number') {
          next.availableMinutes = String(value)
          d.available = true
        }
        break
      case 'city':
        if (typeof value === 'string') {
          next.city = value
          d.city = true
        }
        break
      case 'currency':
        if (typeof value === 'string') {
          next.currency = value
          d.currency = true
        }
        break
      case 'start_time':
        if (typeof value === 'string' && /^\d{2}:\d{2}$/.test(value)) {
          next.startTime = value
          d.startTime = true
        }
        break
      case 'mood':
        if (typeof value === 'string') {
          next.mood = value
          d.mood = true
        }
        break
      case 'neighbourhood':
        next.neighbourhood = typeof value === 'string' ? value : ''
        d.neighbourhood = true
        break
      case 'group_size':
        if (typeof value === 'number') {
          next.groupSize = String(value)
          d.groupSize = true
        }
        break
      case 'interests':
        if (Array.isArray(value)) {
          next.interests = value.filter((x): x is string => typeof x === 'string')
          d.interests = true
        }
        break
      case 'constraints':
        if (Array.isArray(value)) {
          const texts = value.filter((x): x is string => typeof x === 'string')
          const toggles = { ...NO_TOGGLES }
          const extra: string[] = []
          for (const t of texts) {
            const match = CONSTRAINT_TOGGLES.find((c) => c.text.toLowerCase() === t.toLowerCase())
            if (match) toggles[match.id] = true
            else extra.push(t)
          }
          next.toggles = toggles
          next.extraConstraints = extra.join(', ')
          d.constraints = true
        }
        break
      case 'free_text':
        next.freeText = typeof value === 'string' ? value : ''
        break
      case 'relaxed_constraints':
        if (Array.isArray(value)) {
          next.relaxed = unionStrings(next.relaxed, value)
          // Untick matching toggles. Not marked as edited: in description mode the toggles stay unsent and the
          // drop travels as relaxed_constraints; in form-only mode the toggles are sent and now omit it.
          for (const t of CONSTRAINT_TOGGLES) if (next.relaxed.includes(t.key)) next.toggles[t.id] = false
        }
        break
      default:
        break
    }
  }
  return { form: next, dirty: d }
}

/** "15:00" -> minutes, for display. */
export function timeToMinutes(hhmm: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm)
  if (!m) return null
  return Number(m[1]) * 60 + Number(m[2])
}
