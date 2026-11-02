import type { CheckItem } from '../types'
import { humanize } from '../lib/format'
import { Icon, type IconName } from './Icon'

/** The names `summarize_checks` emits (backend/app/domain/validator.py); anything else is humanized. */
const CHECK_NAMES: Record<string, string> = {
  budget: 'Budget',
  time: 'Time',
  opening_hours: 'Opening hours',
  diet: 'Diet',
  accessibility: 'Accessibility',
  crowds: 'Crowds (heuristic)',
  weather: 'Weather',
  sources: 'Sources',
}

const STATUS: Record<CheckItem['status'], { label: string; icon: IconName; cls: string }> = {
  pass: { label: 'Pass', icon: 'check', cls: 'border-emerald-200 bg-emerald-50 text-emerald-800' },
  warn: { label: 'Warning', icon: 'alert', cls: 'border-amber-200 bg-amber-50 text-amber-900' },
  unverified: { label: 'Unverified', icon: 'help', cls: 'border-stone-300 bg-stone-100 text-stone-800' },
  fail: { label: 'Failed', icon: 'x', cls: 'border-red-200 bg-red-50 text-red-800' },
}

export function ChecksPanel({ checks }: { checks: CheckItem[] }) {
  if (!checks.length) return null
  return (
    <section aria-labelledby="checks-title" className="rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-5">
      <h3 id="checks-title" className="text-base font-semibold text-stone-900">
        Checks
      </h3>
      <p className="text-sm text-stone-600">Recomputed by the server from the data, not taken from the model.</p>
      <ul className="mt-3 divide-y divide-stone-100">
        {checks.map((c, i) => {
          const s = STATUS[c.status] ?? STATUS.unverified
          return (
            <li key={`${c.name}-${i}`} className="flex flex-col gap-1 py-2 sm:flex-row sm:items-start sm:gap-3">
              <span
                className={`inline-flex w-fit shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-semibold sm:w-28 ${s.cls}`}
              >
                <Icon name={s.icon} className="h-3.5 w-3.5" />
                {s.label}
              </span>
              <span className="min-w-0 text-sm">
                <span className="font-medium text-stone-900">{CHECK_NAMES[c.name] ?? humanize(c.name)}</span>
                {c.detail ? <span className="text-stone-700">{` · ${c.detail}`}</span> : null}
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
