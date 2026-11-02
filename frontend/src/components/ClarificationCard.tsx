import { useState, type Ref } from 'react'
import { ANSWER_MAX } from '../api/client'
import type { ClarificationQuestion } from '../types'
import { Icon } from './Icon'
import { btnPrimary, cardClass, inputClass } from './ui'

export function ClarificationCard({
  questions,
  message,
  busy = false,
  onSubmit,
  headingRef,
}: {
  questions: ClarificationQuestion[]
  message?: string | null
  busy?: boolean
  onSubmit: (answers: Record<string, string>) => void
  headingRef?: Ref<HTMLHeadingElement>
}) {
  const [chosen, setChosen] = useState<Record<string, string>>({})
  const [typed, setTyped] = useState<Record<string, string>>({})

  const answerFor = (q: ClarificationQuestion) =>
    ((typed[q.id] ?? '').trim() || chosen[q.id] || '').slice(0, ANSWER_MAX)
  const complete = questions.length > 0 && questions.every((q) => answerFor(q) !== '')

  return (
    <section aria-labelledby="clarify-title" className={`${cardClass} border-orange-200`}>
      <h2 id="clarify-title" ref={headingRef} tabIndex={-1} className="flex items-center gap-2 text-xl font-bold text-stone-900">
        <Icon name="help" className="h-5 w-5 text-orange-700" />
        {questions.length === 1 ? 'One quick question' : questions.length === 2 ? 'Two quick questions' : 'A few quick questions'}
      </h2>
      <p className="mt-1 text-sm text-stone-700">
        {message || 'I need this before I can plan, so I don’t have to guess.'}
      </p>
      <form
        className="mt-4 space-y-5"
        onSubmit={(e) => {
          e.preventDefault()
          if (!complete || busy) return
          const answers: Record<string, string> = {}
          for (const q of questions) answers[q.id] = answerFor(q)
          onSubmit(answers)
        }}
      >
        {questions.map((q) => {
          const inputId = `clarify-${q.id}-free`
          return (
            <fieldset key={q.id} className="space-y-2">
              <legend className="text-base font-semibold text-stone-900">{q.question}</legend>
              {q.options.length ? (
                <div className="flex flex-wrap gap-2" role="group" aria-label="Quick replies">
                  {q.options.map((opt) => {
                    const active = chosen[q.id] === opt && !(typed[q.id] ?? '').trim()
                    return (
                      <button
                        key={opt}
                        type="button"
                        aria-pressed={active}
                        onClick={() => {
                          setChosen((c) => ({ ...c, [q.id]: opt }))
                          setTyped((t) => ({ ...t, [q.id]: '' }))
                        }}
                        className={`rounded-full border px-3.5 py-1.5 text-sm font-medium transition-colors ${
                          active
                            ? 'border-orange-700 bg-orange-700 text-white'
                            : 'border-stone-300 bg-white text-stone-800 hover:border-orange-400 hover:bg-orange-50'
                        }`}
                      >
                        {opt}
                      </button>
                    )
                  })}
                </div>
              ) : null}
              <div>
                <label htmlFor={inputId} className="text-sm text-stone-700">
                  {q.options.length ? 'Or type your own answer' : 'Your answer'}
                </label>
                <input
                  id={inputId}
                  className={inputClass}
                  value={typed[q.id] ?? ''}
                  maxLength={ANSWER_MAX}
                  onChange={(e) => setTyped((t) => ({ ...t, [q.id]: e.target.value }))}
                />
              </div>
            </fieldset>
          )
        })}
        <button type="submit" className={btnPrimary} disabled={!complete || busy}>
          {busy ? 'Planning…' : 'Continue planning'}
        </button>
      </form>
    </section>
  )
}
