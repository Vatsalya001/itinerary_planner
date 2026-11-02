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
  const [kind, value] = key.split(':', 2)
  if (value && kind === 'must_include') return `Must include: ${humanize(value).toLowerCase()}`
  if (value && kind === 'must_avoid') return `Avoid: ${humanize(value).toLowerCase()}`
  return humanize(value ?? key)
}
