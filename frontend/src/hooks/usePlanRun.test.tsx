import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { describeError } from '../lib/describeError'
import { fixtures } from '../test/utils'
import type { PlanResult } from '../types'
import { usePlanRun, type RunStarter } from './usePlanRun'

/** A starter whose outcome the test controls; it records the signal it was given. */
function controllable() {
  let resolve!: (r: PlanResult) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<PlanResult>((res, rej) => {
    resolve = res
    reject = rej
  })
  const box: { signal: AbortSignal | null } = { signal: null }
  const starter: RunStarter = (_onEvent, signal) => {
    box.signal = signal
    return promise
  }
  return { starter, resolve, reject, box }
}

/** Rejects like fetch does when its signal is aborted, but only when the hook aborts it. */
function abortable() {
  const box: { signal: AbortSignal | null } = { signal: null }
  const starter: RunStarter = (_onEvent, signal) => {
    box.signal = signal
    return new Promise<PlanResult>((_res, rej) => {
      signal.addEventListener('abort', () => rej(new DOMException('The operation was aborted.', 'AbortError')))
    })
  }
  return { starter, box }
}

describe('usePlanRun', () => {
  it('surfaces an AbortError it did not cause instead of staying on "running"', async () => {
    const { result } = renderHook(() => usePlanRun())
    const c = controllable()
    let done!: Promise<void>
    act(() => {
      done = result.current.run(c.starter)
    })
    expect(result.current.phase.kind).toBe('running')
    await act(async () => {
      c.reject(new DOMException('The user aborted a request.', 'AbortError'))
      await done
    })
    expect(c.box.signal?.aborted).toBe(false)
    expect(result.current.phase.kind).toBe('error')
    const phase = result.current.phase as { kind: 'error'; error: unknown }
    expect(describeError(phase.error)).toMatchObject({
      title: 'The plan was interrupted',
      message: 'The connection was cut off before the plan was ready. Please try again.',
    })
  })

  it('surfaces a TimeoutError as an error', async () => {
    const { result } = renderHook(() => usePlanRun())
    const c = controllable()
    let done!: Promise<void>
    act(() => {
      done = result.current.run(c.starter)
    })
    await act(async () => {
      c.reject(new DOMException('signal timed out', 'TimeoutError'))
      await done
    })
    expect(result.current.phase.kind).toBe('error')
    const phase = result.current.phase as { kind: 'error'; error: unknown }
    expect(describeError(phase.error).message).toBe('The planner took too long to answer. Please try again.')
  })

  it('ignores the abort it caused on cancel(), even if the request settles later', async () => {
    const { result } = renderHook(() => usePlanRun())
    const a = abortable()
    let done!: Promise<void>
    act(() => {
      done = result.current.run(a.starter)
    })
    await act(async () => {
      result.current.cancel()
      await done
    })
    expect(a.box.signal?.aborted).toBe(true)
    expect(result.current.phase.kind).toBe('idle')
  })

  it('drops a superseded run: neither its abort nor a late result replaces the new run', async () => {
    const { result } = renderHook(() => usePlanRun())
    const first = controllable()
    const second = controllable()
    let firstDone!: Promise<void>
    let secondDone!: Promise<void>
    act(() => {
      firstDone = result.current.run(first.starter)
    })
    act(() => {
      secondDone = result.current.run(second.starter)
    })
    expect(first.box.signal?.aborted).toBe(true)
    expect(second.box.signal?.aborted).toBe(false)

    // The first run settles late, either way: ignored.
    await act(async () => {
      first.resolve(fixtures.fallback())
      await firstDone
    })
    expect(result.current.phase.kind).toBe('running')

    const plan = fixtures.complete()
    await act(async () => {
      second.resolve(plan)
      await secondDone
    })
    expect(result.current.phase).toEqual({ kind: 'done', result: plan })
  })

  it('ignores trace events from a superseded run', async () => {
    const { result } = renderHook(() => usePlanRun())
    let lateEmit: ((ts: string) => void) | null = null
    const first: RunStarter = (onEvent) => {
      lateEmit = (ts) => onEvent({ type: 'run_started', run_id: 'old', seq: 1, ts })
      return new Promise<PlanResult>(() => {})
    }
    const second = controllable()
    act(() => {
      void result.current.run(first)
    })
    act(() => {
      void result.current.run(second.starter)
    })
    act(() => lateEmit?.('2026-11-05T04:30:00Z'))
    expect(result.current.trace.steps).toHaveLength(0)
  })

  it('aborts the running request on unmount', () => {
    const { result, unmount } = renderHook(() => usePlanRun())
    const a = abortable()
    act(() => {
      void result.current.run(a.starter)
    })
    unmount()
    expect(a.box.signal?.aborted).toBe(true)
  })
})
