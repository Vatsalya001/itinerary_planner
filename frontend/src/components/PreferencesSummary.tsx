import type { Preferences } from '../types'
import { fmtClock, fmtDuration, fmtMoney, humanize } from '../lib/format'
import { constraintName } from '../lib/labels'
import { Badge } from './ui'

const ENERGY: Record<string, string> = { low: 'Low energy', medium: 'Medium energy', high: 'High energy' }

/** "What I understood": the normalised preferences the planner worked from. */
export function PreferencesSummary({ prefs, provisional = false }: { prefs: Preferences; provisional?: boolean }) {
  const chips: Array<{ key: string; text: string; tone?: 'orange' | 'amber' | 'neutral' }> = []
  if (prefs.city) chips.push({ key: 'city', text: prefs.city, tone: 'orange' })
  if (typeof prefs.budget === 'number')
    chips.push({ key: 'budget', text: prefs.currency ? `${fmtMoney(prefs.budget, prefs.currency)} per person` : `${prefs.budget} per person` })
  if (!provisional) chips.push({ key: 'start', text: `From ${fmtClock(prefs.start_min)}` })
  if (!provisional) chips.push({ key: 'time', text: fmtDuration(prefs.available_minutes) })
  chips.push({ key: 'energy', text: ENERGY[prefs.energy] ?? humanize(prefs.energy) })
  if (prefs.neighbourhood) chips.push({ key: 'hood', text: `Near ${prefs.neighbourhood}` })
  if (prefs.group_size > 1) chips.push({ key: 'group', text: `Group of ${prefs.group_size}` })
  prefs.interests.forEach((i) => chips.push({ key: `int-${i}`, text: humanize(i) }))
  prefs.hard_constraints.forEach((c) =>
    chips.push({ key: `c-${c.kind}-${c.value}`, text: constraintName(`${c.kind}:${c.value}`), tone: 'amber' }),
  )

  return (
    <section aria-labelledby="understood-title" className="rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-5">
      <h2 id="understood-title" className="text-sm font-semibold text-stone-900">
        What I understood
      </h2>
      {prefs.mood ? (
        <p className="mt-1 text-sm text-stone-700">
          <span className="text-stone-600">Mood: </span>
          {prefs.mood}
        </p>
      ) : null}
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {chips.map((c) => (
          <li key={c.key}>
            <Badge tone={c.tone ?? 'neutral'}>{c.text}</Badge>
          </li>
        ))}
      </ul>
      {prefs.soft_preferences.length ? (
        <p className="mt-2 text-xs text-stone-600">{`Soft preferences: ${prefs.soft_preferences.join(', ')}`}</p>
      ) : null}
      <p className="mt-2 text-xs text-stone-600">Edit the form above and plan again to change any of these.</p>
    </section>
  )
}
