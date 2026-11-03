import type { Ref } from 'react'
import type { PlanResult } from '../types'
import { classifyDegradation, omissionLabel } from '../lib/labels'
import { legAfter, providersIn } from '../lib/plan'
import { routeUrlFor } from '../lib/urls'
import { ChecksPanel } from './ChecksPanel'
import { DegradationNotices } from './DegradationNotices'
import { ExternalLink } from './ExternalLink'
import { Feedback } from './Feedback'
import { PlanActions } from './PlanActions'
import { LegRow, StopCard } from './StopCard'
import { SummaryBar } from './SummaryBar'
import { Notice, linkClass } from './ui'

function uniq(items: string[]): string[] {
  return [...new Set(items.map((s) => s.trim()).filter(Boolean))]
}

export function PlanView({
  result,
  shareUrl = null,
  onPlanAgain,
  headingRef,
}: {
  result: PlanResult
  shareUrl?: string | null
  onPlanAgain?: () => void
  headingRef?: Ref<HTMLHeadingElement>
}) {
  const plan = result.plan
  if (!plan) {
    return (
      <Notice tone="error" title="This plan couldn't be displayed" role="alert">
        <p>The planner finished but sent no timeline. Please try again.</p>
      </Notice>
    )
  }

  const accessRequired = result.preferences?.hard_constraints.some((c) => c.kind === 'access' && c.value === 'wheelchair') ?? false
  const isFallback = result.status === 'fallback'
  const plannerReasons = result.degradations.filter((d) => classifyDegradation(d) === 'planner')
  const providers = providersIn(plan)
  const warnings = uniq([
    ...result.warnings,
    ...result.violations.filter((v) => v.severity === 'warning').map((v) => v.message),
  ])
  const assumptions = uniq([...result.assumptions, ...(result.preferences?.assumptions ?? [])])

  return (
    <section aria-labelledby="plan-title" className="space-y-4">
      <div className="space-y-1">
        <h2
          id="plan-title"
          ref={headingRef}
          tabIndex={-1}
          className="text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl"
        >
          {`Your Saturday in ${plan.city}`}
        </h2>
        <p className="text-sm text-stone-600">
          {result.mode === 'deterministic'
            ? 'Built by the backup planner (no AI model) and checked by the validator.'
            : result.model
              ? `Planned with ${result.model} and checked by the validator.`
              : 'Checked by the validator before it was shown.'}
        </p>
      </div>

      {isFallback ? (
        <Notice tone="warn" title="This is a backup plan" role="status">
          <p>The AI planner couldn't finish, so a simpler rule-based planner built this plan. It passed the same checks.</p>
          {plannerReasons.length ? (
            <ul className="list-disc pl-5">
              {plannerReasons.map((d, i) => (
                <li key={i}>{d}</li>
              ))}
            </ul>
          ) : null}
        </Notice>
      ) : null}
      <DegradationNotices items={result.degradations} exclude={isFallback ? ['planner'] : []} />

      {plan.totals.available_minutes >= 30 && plan.totals.elapsed_min < 0.8 * plan.totals.available_minutes ? (
        <Notice tone="warn" title="Partial itinerary">
          <p>Only part of your requested window is scheduled. The remaining time is free; this is not a full-window outing.</p>
        </Notice>
      ) : null}
      <SummaryBar plan={plan} accessRequired={accessRequired} />

      {plan.summary_note ? (
        <p className="rounded-xl bg-orange-50 p-3 text-sm text-stone-800">{plan.summary_note}</p>
      ) : null}

      <div>
        <h3 className="sr-only">Timeline</h3>
        <ol className="space-y-0">
          {plan.stops.map((stop, i) => {
            const leg = i < plan.stops.length - 1 ? legAfter(plan, i) : null
            return (
              <li key={`${stop.candidate.id}-${i}`}>
                <StopCard stop={stop} index={i} city={plan.city} accessRequired={accessRequired} />
                {leg ? <LegRow leg={leg} routeUrl={routeUrlFor(plan, leg)} accessRequired={accessRequired} /> : i < plan.stops.length - 1 ? <div className="h-3" /> : null}
              </li>
            )
          })}
        </ol>
      </div>

      {plan.omitted.length ? (
        <section aria-labelledby="omitted-title" className="rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-5">
          <h3 id="omitted-title" className="text-base font-semibold text-stone-900">
            Trade-offs and what I left out
          </h3>
          <ul className="mt-2 space-y-2">
            {plan.omitted.map((o, i) => (
              <li key={i} className="text-sm text-stone-800">
                <span className="font-semibold text-stone-900">{omissionLabel(o)}</span>
                {` — ${o.reason}`}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <ChecksPanel checks={result.checks} />

      {assumptions.length || warnings.length ? (
        <div className="grid gap-4 md:grid-cols-2">
          {assumptions.length ? (
            <section aria-labelledby="assumptions-title" className="rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-5">
              <h3 id="assumptions-title" className="text-base font-semibold text-stone-900">
                Assumptions
              </h3>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-stone-800">
                {assumptions.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            </section>
          ) : null}
          {warnings.length ? (
            <section aria-labelledby="warnings-title" className="rounded-2xl border border-amber-200 bg-amber-50 p-4 sm:p-5">
              <h3 id="warnings-title" className="text-base font-semibold text-amber-950">
                Before you go
              </h3>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-amber-950">
                {warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      ) : null}

      <section aria-labelledby="actions-title" className="rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-5">
        <h3 id="actions-title" className="mb-3 text-base font-semibold text-stone-900">
          Take it with you
        </h3>
        <PlanActions plan={plan} accessRequired={accessRequired} shareUrl={shareUrl} shareId={result.share_id ?? null} onPlanAgain={onPlanAgain} />
        {result.share_id ? (
          <div className="mt-3 border-t border-stone-100 pt-3">
            <Feedback shareId={result.share_id} />
          </div>
        ) : null}
      </section>

      <p className="text-xs text-stone-600">
        {providers.has('google_places') ? (
          <span>
            Place data: <span translate="no">Google Maps</span>.{' '}
          </span>
        ) : null}
        {providers.has('curated_snapshot') ? (
          <span>
            Curated places:{' '}
            <ExternalLink className={linkClass} href="https://www.openstreetmap.org/copyright">
              <span>© OpenStreetMap contributors</span>
            </ExternalLink>
            .{' '}
          </span>
        ) : null}
        {providers.has('fixture') || providers.has('fixture_event') ? <span>Some places are synthetic test data. </span> : null}
        Travel times and fares are estimates. Prices can change; check before you go.
      </p>
    </section>
  )
}
