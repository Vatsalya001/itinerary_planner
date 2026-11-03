import type { ReactNode } from 'react'
import type { CanonicalPlan } from '../types'
import { fmtClockRange, fmtDate, fmtDuration, fmtMoney, weatherDetails } from '../lib/format'
import { Icon, type IconName } from './Icon'

function Tile({ icon, label, value, sub }: { icon: IconName; label: string; value: string; sub?: ReactNode }) {
  return (
    <div className="rounded-xl border border-stone-200 bg-white p-3">
      <dt className="flex items-center gap-1.5 text-xs font-medium text-stone-600">
        <Icon name={icon} className="h-3.5 w-3.5" />
        {label}
      </dt>
      <dd className="mt-1 text-base font-semibold text-stone-900">{value}</dd>
      {sub ? <dd className="mt-0.5 text-xs text-stone-600">{sub}</dd> : null}
    </div>
  )
}

/** Worst case against budget. Fill carries the state; the track is a lighter step of the same hue. */
function BudgetMeter({ worst, budget, currency }: { worst: number; budget: number; currency: string }) {
  const ratio = budget > 0 ? worst / budget : worst > 0 ? Infinity : 0
  const pct = Math.min(100, Math.round(ratio * 100))
  const tone =
    ratio > 1
      ? { fill: 'bg-red-600', track: 'bg-red-100', text: 'Over budget', icon: 'alert' as const }
      : ratio >= 0.85
        ? { fill: 'bg-amber-500', track: 'bg-amber-100', text: 'Close to budget', icon: 'alert' as const }
        : { fill: 'bg-emerald-600', track: 'bg-emerald-100', text: 'Within estimated budget', icon: 'check' as const }
  return (
    <div className="mt-2">
      <div
        role="meter"
        aria-label="Worst-case estimate as a share of your budget"
        aria-valuemin={0}
        aria-valuemax={budget}
        aria-valuenow={Math.min(worst, budget)}
        aria-valuetext={`${fmtMoney(worst, currency)} of ${fmtMoney(budget, currency)}`}
        className={`h-1.5 w-full overflow-hidden rounded-full ${tone.track}`}
      >
        <div className={`h-full rounded-full ${tone.fill}`} style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 flex items-center gap-1 text-xs text-stone-700">
        <Icon name={tone.icon} className="h-3.5 w-3.5" />
        {tone.text}
      </p>
    </div>
  )
}

export function SummaryBar({ plan }: { plan: CanonicalPlan }) {
  const t = plan.totals
  const hasBudget = typeof t.budget === 'number'
  const rainy = plan.weather.status === 'ok' && (plan.weather.precip_prob_max ?? 0) >= 50
  return (
    <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
      <Tile
        icon="calendar"
        label="When"
        value={fmtClockRange(plan.start_min, plan.end_min)}
        sub={`${fmtDate(plan.date)} · ${fmtDuration(t.elapsed_min)}`}
      />
      <Tile
        icon={rainy ? 'rain' : 'sun'}
        label="Weather"
        value={plan.weather.status === 'ok' ? plan.weather.summary || 'Forecast checked' : 'Weather not checked'}
        sub={plan.weather.status === 'ok' ? weatherDetails(plan.weather) : 'Check the forecast before you go'}
      />
      <div className="rounded-xl border border-stone-200 bg-white p-3">
        <dt className="flex items-center gap-1.5 text-xs font-medium text-stone-600">
          <Icon name="wallet" className="h-3.5 w-3.5" />
          Worst-case estimate per person
        </dt>
        <dd className="mt-1 text-base font-semibold text-stone-900">
          {fmtMoney(t.worst_case, t.currency)}
          {hasBudget ? (
            <span className="font-normal text-stone-600">{` of ${fmtMoney(t.budget as number, t.currency)}`}</span>
          ) : null}
        </dd>
        <dd className="mt-0.5 text-xs text-stone-600">
          {`Includes ${fmtMoney(t.contingency, t.currency)} contingency (10%)`}
        </dd>
        {hasBudget ? (
          <dd>
            <BudgetMeter worst={t.worst_case} budget={t.budget as number} currency={t.currency} />
          </dd>
        ) : null}
      </div>
      <Tile
        icon="clock"
        label="Left over"
        value={typeof t.budget_left === 'number' ? `${fmtMoney(t.budget_left, t.currency)} spare` : 'No budget set'}
        sub={`${fmtDuration(Math.max(0, t.minutes_left))} spare of ${fmtDuration(t.available_minutes)} · ${fmtDuration(t.walking_min)} walking`}
      />
    </dl>
  )
}
