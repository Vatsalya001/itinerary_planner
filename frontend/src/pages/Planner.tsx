import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import { postPlan, resumePlan } from '../api/sse'
import { ApiError } from '../api/errors'
import { ItineraryHistory } from '../components/ItineraryHistory'
import { useItineraryHistory } from '../hooks/useItineraryHistory'
import { ClarificationCard } from '../components/ClarificationCard'
import { ErrorCard } from '../components/ErrorCard'
import { InfeasibleCard } from '../components/InfeasibleCard'
import { PlannerForm, type FormErrors } from '../components/PlannerForm'
import { PlanView } from '../components/PlanView'
import { PreferencesSummary } from '../components/PreferencesSummary'
import { TracePanel } from '../components/TracePanel'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { usePlanRun, type RunStarter } from '../hooks/usePlanRun'
import { describeError } from '../lib/describeError'
import {
  buildRequest,
  defaultFormState,
  patchForm,
  validateForm,
  type DirtyFields,
  type FieldKey,
  type FormState,
} from '../lib/form'
import { applyPatch } from '../lib/plan'
import { runStatusText } from '../lib/runStatus'
import { shareUrlFor } from '../lib/urls'
import type { PlanRequest, PlanResult, Relaxation } from '../types'

export default function Planner({ active = true }: { active?: boolean }) {
  const Heading = active ? 'h1' : 'h2'
  useDocumentTitle('Perfect Saturday Planner')
  const [form, setForm] = useState<FormState>(defaultFormState)
  const [dirty, setDirty] = useState<DirtyFields>({})
  const [errors, setErrors] = useState<FormErrors>({})
  const { trace, phase, run, cancel } = usePlanRun()
  const history = useItineraryHistory()
  const lastSavedRun = useRef<string | null>(null)
  const lastRequest = useRef<PlanRequest | null>(null)
  const [submittedCurrency, setSubmittedCurrency] = useState<string | null>(null)
  const [resumed, setResumed] = useState(false)
  const lastStarter = useRef<{ starter: RunStarter; resetTrace: boolean } | null>(null)
  const resultHeading = useRef<HTMLHeadingElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)
  const busy = phase.kind === 'running'

  const start = useCallback(
    (starter: RunStarter, resetTrace = true) => {
      lastStarter.current = { starter, resetTrace }
      setResumed(!resetTrace)
      void run(starter, { resetTrace })
    },
    [run],
  )

  const submitRequest = useCallback(
    (request: PlanRequest) => {
      lastRequest.current = request
      setSubmittedCurrency(request.currency ?? null)
      start((onEvent, signal) => postPlan(request, onEvent, signal))
    },
    [start],
  )

  const saveHistory = history.save
  useEffect(() => {
    if (phase.kind !== 'done' || !phase.result.plan
      || !['complete', 'fallback'].includes(phase.result.status)
      || lastSavedRun.current === phase.result.run_id) return
    lastSavedRun.current = phase.result.run_id
    saveHistory(phase.result, lastRequest.current)
  }, [phase, saveHistory])

  // Move focus to the outcome so keyboard and screen-reader users land on it.
  useEffect(() => {
    if (phase.kind === 'done' || phase.kind === 'error') resultHeading.current?.focus()
  }, [phase])

  const onChange = (next: FormState, field?: FieldKey) => {
    setForm(next)
    if (!field && next.freeText !== form.freeText) setDirty({})
    if (field) setDirty((d) => (d[field] ? d : { ...d, [field]: true }))
  }

  const onSubmit = () => {
    const errs = validateForm(form)
    setErrors(errs)
    if (Object.keys(errs).length) return
    submitRequest(buildRequest(form, dirty))
  }

  const onRelax = (relaxation: Relaxation) => {
    const base = lastRequest.current ?? buildRequest(form, dirty)
    const synced = patchForm(form, dirty, relaxation.patch)
    setForm(synced.form)
    setDirty(synced.dirty)
    submitRequest(applyPatch(base, relaxation.patch))
  }

  const onAnswer = (threadId: string, answers: Record<string, string>) => {
    const existing = lastRequest.current ?? {}
    lastRequest.current = { ...existing, free_text: [existing.free_text, ...Object.values(answers)].filter(Boolean).join('. ') }
    start((onEvent, signal) => resumePlan(threadId, answers, onEvent, signal), false)
  }

  const onRetry = () => {
    const last = lastStarter.current
    if (last) start(last.starter, last.resetTrace)
  }

  const onPlanAgain = () => {
    window.scrollTo({ top: 0, behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
    textarea.current?.focus({ preventScroll: true })
  }

  const result: PlanResult | null = phase.kind === 'done' ? phase.result : null

  return (
    <div className="space-y-6">
      {/* Always mounted so assistive tech registers it before the first announcement. */}
      <p role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-testid="run-status">
        {runStatusText(phase, resumed, trace.steps.length > 0)}
      </p>
      <div className="space-y-2">
        <Heading className="text-3xl font-bold tracking-tight text-stone-900 sm:text-4xl">Plan a Saturday that fits how you feel</Heading>
        <p className="max-w-2xl text-stone-700">
          Tell me how you feel and what you've got. You get real places, real clock times and the weather, a reason for
          every stop, and honesty about what couldn't be checked.
        </p>
      </div>

      <PlannerForm
        form={form}
        dirty={dirty}
        errors={errors}
        busy={busy}
        onChange={onChange}
        onSubmit={onSubmit}
        onCancel={cancel}
        onReset={() => {
          setForm(defaultFormState())
          setDirty({})
          setErrors({})
        }}
        textareaRef={textarea}
      />

      <ItineraryHistory entries={history.entries} notice={history.notice} busy={busy}
        onRemove={history.remove} onClear={history.clear} />

      {result?.preferences && result.status !== 'rejected' ? <PreferencesSummary prefs={result.preferences} provisional={result.status === 'needs_clarification'} /> : null}

      <TracePanel trace={trace} running={busy} startedAt={phase.kind === 'running' ? phase.startedAt : null} />


      {phase.kind === 'error' ? (
        <ErrorCard {...describeError(phase.error)} onRetry={phase.error instanceof ApiError && [400, 413, 422].includes(phase.error.status) ? undefined : onRetry} headingRef={resultHeading} />
      ) : null}

      {result ? (
        <ResultView
          result={result}
          busy={busy}
          currency={result.preferences?.currency ?? submittedCurrency}
          headingRef={resultHeading}
          onAnswer={onAnswer}
          onRelax={onRelax}
          onPlanAgain={onPlanAgain}
          onRetry={onRetry}
        />
      ) : null}
    </div>
  )
}

function ResultView({
  result,
  busy,
  currency,
  headingRef,
  onAnswer,
  onRelax,
  onPlanAgain,
  onRetry,
}: {
  result: PlanResult
  busy: boolean
  currency: string | null
  headingRef: RefObject<HTMLHeadingElement | null>
  onAnswer: (threadId: string, answers: Record<string, string>) => void
  onRelax: (r: Relaxation) => void
  onPlanAgain: () => void
  onRetry: () => void
}) {
  switch (result.status) {
    case 'complete':
    case 'fallback':
      return (
        <PlanView
          result={result}
          shareUrl={result.share_id ? shareUrlFor(result.share_id) : null}
          onPlanAgain={onPlanAgain}
          headingRef={headingRef}
        />
      )
    case 'needs_clarification': {
      const threadId = result.thread_id
      if (!threadId || !result.questions.length) {
        return (
          <ErrorCard
            title="I need a bit more detail"
            message={result.message || 'Please add the city and what you have in mind, then plan again.'}
            headingRef={headingRef}
          />
        )
      }
      return (
        <ClarificationCard
          key={result.run_id || threadId}
          questions={result.questions}
          message={result.message}
          busy={busy}
          onSubmit={(answers) => onAnswer(threadId, answers)}
          headingRef={headingRef}
        />
      )
    }
    case 'no_feasible_plan':
      return (
        <InfeasibleCard
          report={result.infeasible}
          message={result.message}
          currency={currency}
          busy={busy}
          onApply={onRelax}
          headingRef={headingRef}
        />
      )
    case 'rejected':
    default:
      return (
        <ErrorCard
          title={/something went wrong|planning took too long/i.test(result.message ?? '') ? 'The plan was interrupted' : "I can't plan that"}
          onRetry={/something went wrong|planning took too long/i.test(result.message ?? '') ? onRetry : undefined}
          message={
            result.message ||
            result.violations.find((v) => v.severity === 'error')?.message ||
            'This request is outside what the planner can do.'
          }
          runId={result.run_id || null}
          headingRef={headingRef}
        />
      )
  }
}
