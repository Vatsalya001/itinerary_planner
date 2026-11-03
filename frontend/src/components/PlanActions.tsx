import { useEffect, useRef, useState } from 'react'
import type { CanonicalPlan } from '../types'
import { downloadIcs } from '../lib/ics'
import { copyText, planToText, whatsappUrl } from '../lib/share'
import { routeUrlFor } from '../lib/urls'
import { ExternalLink } from './ExternalLink'
import { Icon } from './Icon'
import { btnPrimary, btnSecondary } from './ui'

export function PlanActions({
  plan,
  shareUrl,
  shareId,
  onPlanAgain,
  accessRequired = false,
}: {
  plan: CanonicalPlan
  shareUrl: string | null
  shareId: string | null
  onPlanAgain?: () => void
  accessRequired?: boolean
}) {
  const [status, setStatus] = useState('')
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])

  function flash(text: string) {
    setStatus(text)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setStatus(''), 4000)
  }

  const routeUrl = routeUrlFor(plan)
  const text = planToText(plan, { shareUrl, accessRequired })

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {routeUrl ? (
          <ExternalLink className={btnPrimary} href={routeUrl}>
            <Icon name="route" />
            Open full route in Google Maps
          </ExternalLink>
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
        <ExternalLink className={btnSecondary} href={whatsappUrl(text)}>
          <Icon name="message" />
          Share on WhatsApp
        </ExternalLink>
        <button
          type="button"
          className={btnSecondary}
          onClick={() =>
            flash(
              downloadIcs(plan, { shareUrl, uidSeed: shareId })
                ? 'Calendar file downloaded.'
                : "Couldn't create the calendar file. Please try again.",
            )
          }
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
