import { useEffect, useRef, useState } from 'react'
import type { CanonicalPlan } from '../types'
import { downloadIcs } from '../lib/ics'
import { copyText, planToText, whatsappUrl } from '../lib/share'
import { safeHttpsUrl } from '../lib/urls'
import { Icon } from './Icon'
import { btnPrimary, btnSecondary } from './ui'

export function PlanActions({
  plan,
  shareUrl,
  shareId,
  onPlanAgain,
}: {
  plan: CanonicalPlan
  shareUrl: string | null
  shareId: string | null
  onPlanAgain?: () => void
}) {
  const [status, setStatus] = useState('')
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])

  function flash(text: string) {
    setStatus(text)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setStatus(''), 4000)
  }

  const routeUrl = safeHttpsUrl(plan.maps_route_url)
  const text = planToText(plan, { shareUrl })

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {routeUrl ? (
          <a className={btnPrimary} href={routeUrl} target="_blank" rel="noopener noreferrer">
            <Icon name="route" />
            Open full route in Google Maps
            <span className="sr-only"> (opens in a new tab)</span>
          </a>
        ) : null}
        <button
          type="button"
          className={btnSecondary}
          onClick={async () => flash((await copyText(text)) ? 'Plan copied as text.' : "Couldn't copy. Please try again.")}
        >
          <Icon name="copy" />
          Copy plan
        </button>
        {shareUrl ? (
          <button
            type="button"
            className={btnSecondary}
            onClick={async () => flash((await copyText(shareUrl)) ? 'Share link copied.' : "Couldn't copy the link.")}
          >
            <Icon name="link" />
            Copy share link
          </button>
        ) : null}
        <a className={btnSecondary} href={whatsappUrl(text)} target="_blank" rel="noopener noreferrer">
          <Icon name="message" />
          Share on WhatsApp
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
        <button
          type="button"
          className={btnSecondary}
          onClick={() => {
            downloadIcs(plan, { shareUrl, uidSeed: shareId })
            flash('Calendar file downloaded.')
          }}
        >
          <Icon name="calendar" />
          Add to calendar
        </button>
        {onPlanAgain ? (
          <button type="button" className={btnSecondary} onClick={onPlanAgain}>
            Plan again with changes
          </button>
        ) : null}
      </div>
      <p role="status" aria-live="polite" className="min-h-5 text-sm text-stone-700">
        {status}
      </p>
    </div>
  )
}
