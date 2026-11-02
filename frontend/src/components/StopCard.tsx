import type { Candidate, Leg, ScheduledStop } from '../types'
import {
  categoryLabel,
  costBasisLabel,
  fmtClock,
  fmtDuration,
  fmtMoneyRange,
  modeLabel,
  sourceLabel,
} from '../lib/format'
import { mapsUrlFor, safeHttpsUrl } from '../lib/urls'
import { ExternalLink } from './ExternalLink'
import { Icon, type IconName } from './Icon'
import { Badge, linkClass } from './ui'

const SOURCE_TONES = {
  google_places: 'green',
  curated_snapshot: 'blue',
  web_event: 'amber',
  fixture: 'violet',
  fixture_event: 'violet',
} as const

export function SourceBadge({ candidate }: { candidate: Candidate }) {
  const provider = candidate.source.provider
  return <Badge tone={SOURCE_TONES[provider] ?? 'neutral'}>{sourceLabel(provider)}</Badge>
}

/** Provider attribution shown next to each place: Google Maps text, OSM copyright, or the event site. */
export function Attribution({ candidate }: { candidate: Candidate }) {
  const src = candidate.source
  if (src.provider === 'google_places') {
    return (
      <span className="text-xs text-stone-600" translate="no">
        Google Maps
      </span>
    )
  }
  if (src.provider === 'curated_snapshot') {
    return (
      <span className="text-xs text-stone-600">
        <ExternalLink className={linkClass} href="https://www.openstreetmap.org/copyright">
          <span>© OpenStreetMap contributors</span>
        </ExternalLink>
        {src.last_checked ? <span>{` · checked ${src.last_checked}`}</span> : null}
      </span>
    )
  }
  if (src.provider === 'web_event') {
    const url = safeHttpsUrl(src.url)
    const site = src.attribution || (url ? new URL(url).hostname : null)
    return (
      <span className="text-xs text-stone-600">
        {url ? (
          <ExternalLink className={linkClass} href={url}>
            <span>{site ? `Event page on ${site}` : 'Event page'}</span>
          </ExternalLink>
        ) : site ? (
          <span>{`Source: ${site}`}</span>
        ) : null}
      </span>
    )
  }
  if (src.attribution) return <span className="text-xs text-stone-600">{src.attribution}</span>
  return null
}

function CostLine({ candidate }: { candidate: Candidate }) {
  const cost = candidate.cost
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm text-stone-800">
      {cost.basis === 'free' && cost.max <= 0 ? (
        <span className="sr-only">Cost:</span>
      ) : (
        <>
          <span className="font-semibold">{fmtMoneyRange(cost.min, cost.max, cost.currency)}</span>
          {cost.max > 0 ? <span className="text-stone-600">per person</span> : null}
        </>
      )}
      <Badge
        tone={
          cost.basis === 'listed_price_range' || cost.basis === 'ticket_price' || cost.basis === 'free' ? 'green' : 'neutral'
        }
      >
        {costBasisLabel(cost.basis)}
      </Badge>
    </p>
  )
}

function MapsLink({ candidate, city, label = 'Open in Google Maps' }: { candidate: Candidate; city: string; label?: string }) {
  return (
    <ExternalLink
      className="inline-flex items-center gap-1 text-sm font-medium text-orange-800 underline decoration-orange-300 underline-offset-2 hover:decoration-orange-700"
      href={mapsUrlFor(candidate, city)}
    >
      <Icon name="pin" className="h-4 w-4" />
      {label}
      <span className="sr-only">{` for ${candidate.name}`}</span>
    </ExternalLink>
  )
}

export function StopCard({ stop, index, city }: { stop: ScheduledStop; index: number; city: string }) {
  const c = stop.candidate
  const headingId = `stop-${index}-${c.id.replace(/[^A-Za-z0-9_-]/g, '-')}`
  return (
    <article aria-labelledby={headingId} className="rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-5">
      <div className="flex gap-3 sm:gap-4">
        <div className="w-16 shrink-0 text-right sm:w-24">
          <p className="text-sm font-semibold text-stone-900">{fmtClock(stop.start_min)}</p>
          <p className="text-xs text-stone-600">{`to ${fmtClock(stop.end_min)}`}</p>
          <p className="mt-1 text-xs text-stone-600">{fmtDuration(stop.duration_min)}</p>
        </div>
        <div className="min-w-0 flex-1 space-y-2 border-l border-orange-200 pl-3 sm:pl-4">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-orange-800">
              {`Stop ${index + 1} · ${categoryLabel(c.category)}${c.kind === 'event' && c.category !== 'event' ? ' · Event' : ''}`}
            </p>
            <h4 id={headingId} className="text-lg font-semibold leading-snug text-stone-900">
              {c.name}
            </h4>
            {c.address || c.neighbourhood ? (
              <p className="text-sm text-stone-600">{c.address || c.neighbourhood}</p>
            ) : null}
          </div>

          <CostLine candidate={c} />

          {stop.why ? <p className="text-sm text-stone-800">{stop.why}</p> : null}
          {stop.tradeoff ? (
            <p className="text-sm text-stone-700">
              <span className="font-semibold text-stone-800">Trade-off: </span>
              {stop.tradeoff}
            </p>
          ) : null}

          {stop.warnings.length ? (
            <ul className="space-y-1 text-sm text-amber-900">
              {stop.warnings.map((w, i) => (
                <li key={i} className="flex gap-1.5">
                  <Icon name="alert" className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{w}</span>
                </li>
              ))}
            </ul>
          ) : null}

          {stop.plan_b ? (
            <div className="rounded-xl border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950">
              <p className="flex items-center gap-1.5 font-semibold">
                <Icon name="umbrella" className="h-4 w-4" />
                Plan B if it rains
              </p>
              <p className="mt-1">
                <span className="font-medium">{stop.plan_b.name}</span>
                <span className="text-sky-900">{` · ${categoryLabel(stop.plan_b.category)} · ${fmtMoneyRange(
                  stop.plan_b.cost.min,
                  stop.plan_b.cost.max,
                  stop.plan_b.cost.currency,
                )} (${costBasisLabel(stop.plan_b.cost.basis)})`}</span>
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <SourceBadge candidate={stop.plan_b} />
                <Attribution candidate={stop.plan_b} />
                <MapsLink candidate={stop.plan_b} city={city} label="Plan B in Google Maps" />
              </div>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2 pt-1">
            <SourceBadge candidate={c} />
            <Attribution candidate={c} />
            <MapsLink candidate={c} city={city} />
          </div>
        </div>
      </div>
    </article>
  )
}

const MODE_ICONS: Record<string, IconName> = { walk: 'walk', auto: 'auto', cab: 'cab', metro: 'metro' }

export function LegRow({ leg }: { leg: Leg }) {
  const fare =
    leg.fare.max > 0 ? `${fmtMoneyRange(leg.fare.min, leg.fare.max, leg.fare.currency)} fare` : 'no fare'
  const minutes = leg.minutes_max > leg.minutes ? `${leg.minutes}–${leg.minutes_max} min` : `${leg.minutes} min`
  return (
    <div className="flex items-center gap-2 py-2 pl-[4.5rem] text-sm text-stone-700 sm:pl-[7.5rem]">
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-stone-100 text-stone-700">
        <Icon name={MODE_ICONS[leg.mode] ?? 'route'} className="h-4 w-4" />
      </span>
      <span>
        <span className="font-medium text-stone-900">{modeLabel(leg.mode)}</span>
        {` · ${minutes} · ${leg.distance_km.toFixed(1)} km · ${fare}`}
      </span>
      <Badge>estimate</Badge>
    </div>
  )
}
