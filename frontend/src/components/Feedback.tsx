import { useId, useState } from 'react'
import { FEEDBACK_REASON_MAX, sendFeedback } from '../api/client'
import type { FeedbackRating } from '../types'
import { Icon } from './Icon'
import { btnSecondary, inputClass } from './ui'

type State = 'idle' | 'asking' | 'sending' | 'sent' | 'error'

export function Feedback({ shareId }: { shareId: string }) {
  const [state, setState] = useState<State>('idle')
  const [rating, setRating] = useState<FeedbackRating | null>(null)
  const [reason, setReason] = useState('')
  const reasonId = useId()

  async function submit(r: FeedbackRating, why?: string) {
    setRating(r)
    setState('sending')
    try {
      await sendFeedback(shareId, r, why)
      setState('sent')
    } catch {
      setState('error')
    }
  }

  if (state === 'sent') {
    return (
      <p role="status" className="text-sm text-stone-700">
        Thanks for the feedback.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-stone-700">Was this plan useful?</span>
        <button
          type="button"
          className={btnSecondary}
          aria-pressed={rating === 'up'}
          disabled={state === 'sending'}
          onClick={() => void submit('up')}
        >
          <Icon name="thumbsUp" />
          <span>Yes</span>
        </button>
        <button
          type="button"
          className={btnSecondary}
          aria-pressed={rating === 'down'}
          aria-expanded={state === 'asking'}
          disabled={state === 'sending'}
          onClick={() => {
            setRating('down')
            setState('asking')
          }}
        >
          <Icon name="thumbsDown" />
          <span>Not really</span>
        </button>
      </div>
      {state === 'asking' ? (
        <form
          className="flex flex-col gap-2 sm:flex-row sm:items-end"
          onSubmit={(e) => {
            e.preventDefault()
            void submit('down', reason)
          }}
        >
          <div className="flex-1">
            <label htmlFor={reasonId} className="text-sm font-medium text-stone-800">
              What could be better? (optional)
            </label>
            <input
              id={reasonId}
              className={inputClass}
              value={reason}
              maxLength={FEEDBACK_REASON_MAX}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
          <button type="submit" className={btnSecondary}>
            Send
          </button>
        </form>
      ) : null}
      {state === 'error' ? (
        <p role="alert" className="text-sm text-red-800">
          Couldn't send feedback. Please try again.
        </p>
      ) : null}
    </div>
  )
}
