import { useCallback, useEffect, useRef, useState } from 'react'
import { HISTORY_KEY, HISTORY_LIMIT, historyEntry, readHistory, writeHistory, type HistoryState, type ItineraryHistoryEntry } from '../lib/itineraryHistory'
import type { PlanRequest, PlanResult } from '../types'

function load(): HistoryState {
  try { return readHistory(window.localStorage) }
  catch { return { entries: [], notice: 'Browser storage is unavailable. History will last only in this tab.' } }
}

export function useItineraryHistory() {
  const [state, setState] = useState(load)
  const current = useRef(state)
  const change = useCallback((update: (entries: ItineraryHistoryEntry[]) => ItineraryHistoryEntry[]) => {
    const latest = load()
    const entries = update(latest.notice || current.current.notice ? current.current.entries : latest.entries).slice(0, HISTORY_LIMIT)
    let notice: string | null
    try { notice = writeHistory(window.localStorage, entries) }
    catch { notice = 'Browser storage is unavailable. History changes will last only in this tab.' }
    const next = { entries, notice }
    current.current = next
    setState(next)
  }, [])
  const save = useCallback((result: PlanResult, request: PlanRequest | null) => {
    const item = historyEntry(result, request)
    if (item) change(entries => [item, ...entries.filter(e => e.shareId !== item.shareId)])
    else if (result.plan && ['complete', 'fallback'].includes(result.status) && !result.share_id) {
      const next = { ...current.current, notice: 'This itinerary could not be added to history because its saved-plan link is unavailable. You can still copy the plan.' }
      current.current = next
      setState(next)
    }
  }, [change])
  const remove = useCallback((id: string) => change(entries => entries.filter(e => e.shareId !== id)), [change])
  const clear = useCallback(() => change(() => []), [change])
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key !== HISTORY_KEY && event.key !== null) return
      const next = load()
      current.current = next
      setState(next)
    }
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])
  return { ...state, save, remove, clear }
}
