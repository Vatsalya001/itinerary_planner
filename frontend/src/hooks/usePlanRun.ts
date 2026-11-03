import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import type { TraceListener } from '../api/sse'
import { initialTrace, traceReducer } from '../lib/trace'
import type { PlanResult } from '../types'

export type RunPhase =
  | { kind: 'idle' }
  | { kind: 'running'; startedAt: number }
  | { kind: 'done'; result: PlanResult }
  | { kind: 'error'; error: unknown }

export type RunStarter = (onEvent: TraceListener, signal: AbortSignal) => Promise<PlanResult>

/**
 * Runs a streaming plan request: owns the AbortController, the trace reducer and the result phase.
 *
 * Each run gets its own AbortController, which doubles as the run's token: only this hook aborts it (a new run,
 * `cancel()` or unmount), so a run whose controller is aborted or no longer current was superseded on purpose and
 * its outcome is dropped. Any other failure, including an AbortError or TimeoutError the hook did not cause, ends
 * the run in the `error` phase so the UI never stays stuck on "running".
 */
export function usePlanRun() {
  const [trace, dispatch] = useReducer(traceReducer, initialTrace)
  const [phase, setPhase] = useState<RunPhase>({ kind: 'idle' })
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => controller.current?.abort(), [])

  const run = useCallback(async (starter: RunStarter, options: { resetTrace?: boolean } = {}) => {
    controller.current?.abort()
    const ac = new AbortController()
    controller.current = ac
    /** False once this hook aborted or replaced the run. */
    const live = () => controller.current === ac && !ac.signal.aborted
    if (options.resetTrace !== false) dispatch({ type: 'reset' })
    setPhase({ kind: 'running', startedAt: Date.now() })
    try {
      const result = await starter((event) => {
        if (live()) dispatch({ type: 'event', event })
      }, ac.signal)
      if (live()) setPhase({ kind: 'done', result })
    } catch (error) {
      if (live()) setPhase({ kind: 'error', error })
    }
  }, [])

  const cancel = useCallback(() => {
    controller.current?.abort()
    controller.current = null
    dispatch({ type: 'stop' })
    setPhase({ kind: 'idle' })
  }, [])

  return { trace, phase, run, cancel }
}
