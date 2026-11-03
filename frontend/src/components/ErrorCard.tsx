import { useEffect, useState, type Ref } from 'react'
import { Icon } from './Icon'
import { btnPrimary, cardClass } from './ui'

function useCountdown(seconds: number | null): number {
  const [left, setLeft] = useState(seconds ?? 0)
  const [from, setFrom] = useState(seconds)
  if (from !== seconds) {
    setFrom(seconds)
    setLeft(seconds ?? 0)
  }
  useEffect(() => {
    if (!left) return
    const id = window.setTimeout(() => setLeft((s) => Math.max(0, s - 1)), 1000)
    return () => window.clearTimeout(id)
  }, [left])
  return left
}

export function ErrorCard({
  title,
  message,
  retryAfter = null,
  runId = null,
  onRetry,
  headingRef,
}: {
  title: string
  message: string
  retryAfter?: number | null
  runId?: string | null
  onRetry?: () => void
  headingRef?: Ref<HTMLHeadingElement>
}) {
  const left = useCountdown(retryAfter)
  return (
    <section aria-labelledby="error-title" className={`${cardClass} border-red-200`}>
      <h2 id="error-title" ref={headingRef} tabIndex={-1} className="flex items-center gap-2 text-xl font-bold text-stone-900">
        <Icon name="alert" className="h-5 w-5 text-red-700" />
        {title}
      </h2>
      <p className="mt-2 text-sm text-stone-800">{message}</p>
      {retryAfter !== null ? (
        <p className="mt-1 text-sm text-stone-800">
          {left > 0 ? `Try again in ${left} ${left === 1 ? 'second' : 'seconds'}.` : 'You can try again now.'}
        </p>
      ) : null}
      {runId ? <p className="mt-1 text-xs text-stone-600">{`Reference: ${runId}`}</p> : null}
      {onRetry ? (
        <button type="button" className={`${btnPrimary} mt-4`} onClick={onRetry} disabled={left > 0}>
          Try again
        </button>
      ) : null}
    </section>
  )
}
