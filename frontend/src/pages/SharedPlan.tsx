import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, getSharedPlan } from '../api/client'
import { isAbortError } from '../api/errors'
import { ErrorCard } from '../components/ErrorCard'
import { PlanView } from '../components/PlanView'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { describeError } from '../lib/describeError'
import { shareUrlFor } from '../lib/urls'
import type { PlanResult } from '../types'
import { btnPrimary } from '../components/ui'

type State = { kind: 'loading' } | { kind: 'ok'; result: PlanResult } | { kind: 'error'; error: unknown }

export default function SharedPlan() {
  const { id = '' } = useParams()
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [attempt, setAttempt] = useState(0)
  const [prevKey, setPrevKey] = useState(`${id}:${attempt}`)
  const heading = useRef<HTMLHeadingElement>(null)
  useDocumentTitle(state.kind === 'ok' && state.result.plan ? `Saturday in ${state.result.plan.city}` : 'Shared plan')

  const key = `${id}:${attempt}`
  if (key !== prevKey) {
    setPrevKey(key)
    setState({ kind: 'loading' })
  }

  useEffect(() => {
    const ac = new AbortController()
    getSharedPlan(id, ac.signal)
      .then((result) => setState({ kind: 'ok', result }))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setState({ kind: 'error', error })
      })
    return () => ac.abort()
  }, [id, attempt])

  if (state.kind === 'loading') {
    return (
      <p role="status" className="py-10 text-center text-stone-700">
        Loading the plan…
      </p>
    )
  }

  if (state.kind === 'error') {
    const notFound = state.error instanceof ApiError && state.error.status === 404
    return (
      <div className="space-y-4">
        {notFound ? (
          <ErrorCard
            title="This plan isn't available"
            message="The link may be mistyped, or the plan has expired. Shared plans are kept for 30 days."
          />
        ) : (
          <ErrorCard {...describeError(state.error)} onRetry={() => setAttempt((a) => a + 1)} />
        )}
        <Link to="/" className={btnPrimary}>
          Plan your own Saturday
        </Link>
      </div>
    )
  }

  const { result } = state
  return (
    <div className="space-y-6">
      {result.plan && (result.status === 'complete' || result.status === 'fallback') ? (
        <PlanView result={result} shareUrl={shareUrlFor(id)} headingRef={heading} />
      ) : (
        <ErrorCard
          title="This plan has no timeline"
          message={result.message || 'This link does not point to a finished plan.'}
        />
      )}
      <div className="rounded-2xl border border-orange-200 bg-orange-50 p-4 text-stone-800">
        <p className="font-medium">Want one for yourself?</p>
        <Link to="/" className={`${btnPrimary} mt-2`}>
          Plan your own Saturday
        </Link>
      </div>
    </div>
  )
}
