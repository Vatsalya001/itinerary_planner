import type { Ref } from 'react'
import type { InfeasibleReport, Relaxation } from '../types'
import { fmtDuration, fmtMoney } from '../lib/format'
import { constraintName } from '../lib/labels'
import { Icon } from './Icon'
import { Badge, btnSecondary, cardClass } from './ui'

export function InfeasibleCard({
  report,
  message,
  currency,
  busy = false,
  onApply,
  headingRef,
}: {
  report: InfeasibleReport | null | undefined
  message?: string | null
  currency?: string | null
  busy?: boolean
  onApply: (relaxation: Relaxation) => void
  headingRef?: Ref<HTMLHeadingElement>
}) {
  const reason = report?.reason || message || 'No plan satisfies every hard constraint.'
  const binding = report?.binding_constraints ?? []
  const relaxations = report?.relaxations ?? []
  return (
    <section aria-labelledby="infeasible-title" className={cardClass}>
      <h2 id="infeasible-title" ref={headingRef} tabIndex={-1} className="flex items-center gap-2 text-xl font-bold text-stone-900">
        <Icon name="alert" className="h-5 w-5 text-amber-700" />
        {report ? 'No plan fits everything' : 'Place data is unavailable'}
      </h2>
      <p className="mt-2 text-sm text-stone-800">{reason}</p>
      {message && report?.reason && message !== report.reason ? (
        <p className="mt-1 text-sm text-stone-700">{message}</p>
      ) : null}

      {binding.length ? (
        <div className="mt-3">
          <h3 className="text-sm font-semibold text-stone-900">What's blocking it</h3>
          <ul className="mt-1 flex flex-wrap gap-2">
            {binding.map((b) => (
              <li key={b}>
                <Badge tone="amber">{constraintName(b)}</Badge>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {typeof report?.cheapest_budget === 'number' || typeof report?.min_minutes === 'number' ? (
        <ul className="mt-3 space-y-1 text-sm text-stone-800">
          {typeof report?.cheapest_budget === 'number' ? (
            <li>
              {'Cheapest workable plan I found: '}
              <span className="font-semibold">
                {currency ? fmtMoney(report.cheapest_budget, currency) : String(report.cheapest_budget)}
              </span>
              {' per person (worst case)'}
            </li>
          ) : null}
          {typeof report?.min_minutes === 'number' ? (
            <li>
              {'Shortest workable plan: '}
              <span className="font-semibold">{fmtDuration(report.min_minutes)}</span>
            </li>
          ) : null}
        </ul>
      ) : null}

      {relaxations.length ? (
        <div className="mt-4">
          <h3 className="text-sm font-semibold text-stone-900">Try one of these</h3>
          <div className="mt-2 flex flex-wrap gap-2">
            {relaxations.map((r, i) => (
              <button key={`${r.label}-${i}`} type="button" className={btnSecondary} disabled={busy} onClick={() => onApply(r)}>
                {r.label}
              </button>
            ))}
          </div>
        </div>
      ) : report ? (
        <p className="mt-3 text-sm text-stone-700">Try changing the budget, time or constraints above and plan again.</p>
      ) : null}
    </section>
  )
}
