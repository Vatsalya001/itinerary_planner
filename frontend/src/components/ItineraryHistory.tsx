import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { ItineraryHistoryEntry } from '../lib/itineraryHistory'
import { btnSecondary, cardClass, linkClass } from './ui'

export function ItineraryHistory({ entries, notice, busy, onRemove, onClear }: {
  entries: ItineraryHistoryEntry[]; notice: string | null; busy: boolean
  onRemove: (id: string) => void; onClear: () => void
}) {
  const [confirmClear, setConfirmClear] = useState(false)
  return (
    <section aria-labelledby="history-title" className={cardClass}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="history-title" className="text-lg font-semibold text-stone-900">Your itinerary history</h2>
        {entries.length > 0 ? <button type="button" className={btnSecondary} onClick={() => setConfirmClear(true)}>Clear history</button> : null}
      </div>
      <p className="mt-1 text-xs text-stone-600">History from the last 30 days on this browser, newest first. Your requests are visible to anyone using this browser. Saved-plan links can expire or become unavailable.</p>
      {notice ? <p role="status" className="mt-2 text-sm text-amber-900">{notice}</p> : null}
      {confirmClear && entries.length > 0 ? <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-sm">Remove all history from this browser?</span>
        <button type="button" className={btnSecondary} onClick={() => { onClear(); setConfirmClear(false) }}>Yes, clear history</button>
        <button type="button" className={btnSecondary} onClick={() => setConfirmClear(false)}>Keep history</button>
      </div> : null}
      {entries.length === 0 ? <p className="mt-3 text-sm text-stone-600">Your completed itineraries will appear here. No account needed.</p> : (
        <ul className="mt-3 grid max-h-80 gap-3 overflow-y-auto sm:grid-cols-2" aria-label="Saved itineraries">
          {entries.map(e => <li key={e.shareId} className="min-w-0 rounded-xl border border-stone-200 p-3">
            <p className="break-words font-semibold text-stone-900">{e.city} · {e.date}</p>
            <p className="mt-1 line-clamp-2 break-words text-sm text-stone-600">{e.prompt}</p>
            <details className="mt-1 text-sm text-stone-600">
              <summary className="cursor-pointer">View full request</summary>
              <p className="mt-1 whitespace-pre-wrap break-words">{e.prompt}</p>
            </details>
            <p className="mt-1 text-xs text-stone-500">Saved {new Date(e.savedAt).toLocaleString()}</p>
            <div className="mt-2 flex flex-wrap items-center gap-4">
              {busy ? <span className="text-sm text-stone-500">Finish or cancel planning to open history</span> : <Link className={linkClass} to={`/p/${encodeURIComponent(e.shareId)}`} aria-label={`Open itinerary for ${e.city} on ${e.date}`}>Open itinerary</Link>}
              <button type="button" className="text-sm text-stone-600 underline" aria-label={`Remove itinerary for ${e.city} on ${e.date}`} onClick={() => onRemove(e.shareId)}>Remove</button>
            </div>
          </li>)}
        </ul>
      )}
    </section>
  )
}
