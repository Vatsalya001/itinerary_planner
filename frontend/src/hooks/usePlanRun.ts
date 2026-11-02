import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import { isAbortError } from '../api/errors'
import type { TraceListener } from '../api/sse'
import { initialTrace, traceReducer } from '../lib/trace'
import type { PlanResult } from '../types'

export type RunPhase =
  | { kind: 'idle' }
  | { kind: 'running'; startedAt: number }
  | { kind: 'done'; result: PlanResult }
  | { kind: 'error'; error: unknown }

export type RunStarter = (onEvent: TraceListener, signal: AbortSignal) => Promise<PlanResult>

/** Runs a streaming plan request: owns the AbortController, the trace reducer and the result phase. */
export function usePlanRun() {
  const [trace, dispatch] = useReducer(traceReducer, initialTrace)
  const [phase, setPhase] = useState<RunPhase>({ kind: 'idle' })
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => controller.current?.abort(), [])

  const run = useCallback(async (starter: RunStarter, options: { resetTrace?: boolean } = {}) => {
    controller.current?.abort()
    const ac = new AbortController()
    controller.current = ac
    if (options.resetTrace !== false) dispatch({ type: 'reset' })
    setPhase({ kind: 'running', startedAt: Date.now() })
    try {
      const result = await starter((event) => {
        if (!ac.signal.aborted) dispatch({ type: 'event', event })
      }, ac.signal)
      if (ac.signal.aborted) return
      setPhase({ kind: 'done', result })
    } catch (error) {
      if (ac.signal.aborted || isAbortError(error)) return
      setPhase({ kind: 'error', error })
    }
  }, [])

  const cancel = useCallback(() => {
    controller.current?.abort()
    controller.current = null
    setPhase({ kind: 'idle' })
  }, [])

  return { trace, phase, run, cancel }
}
