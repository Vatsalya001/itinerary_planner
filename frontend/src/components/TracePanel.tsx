import { useEffect, useId, useState } from 'react'
import { fmtMs } from '../lib/format'
import type { StepStatus, TraceState, TraceStep } from '../lib/trace'
import { DegradationNotices } from './DegradationNotices'
import { Icon } from './Icon'

const STATUS_TEXT: Record<StepStatus, string> = {
  running: 'In progress',
  done: 'Done',
  failed: 'Failed',
  warn: 'Needs attention',
  info: 'Note',
}

function StatusIcon({ status }: { status: StepStatus }) {
  switch (status) {
    case 'running':
      return (
        <span className="text-orange-700">
          <Icon name="spinner" className="h-4 w-4 motion-safe:animate-spin" />
        </span>
      )
    case 'done':
      return (
        <span className="text-emerald-700">
          <Icon name="check" />
        </span>
      )
    case 'failed':
      return (
        <span className="text-red-700">
          <Icon name="x" />
        </span>
      )
    case 'warn':
      return (
        <span className="text-amber-700">
          <Icon name="alert" />
        </span>
      )
    default:
      return (
        <span className="text-stone-500">
          <Icon name="dot" />
        </span>
      )
  }
}

function Step({ step }: { step: TraceStep }) {
  return (
    <li className="flex gap-3 py-2">
      <span className="mt-0.5 shrink-0">
        <StatusIcon status={step.status} />
        <span className="sr-only">{STATUS_TEXT[step.status]}: </span>
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="font-medium text-stone-900">{step.label}</span>
          {step.name && (step.kind === 'tool' || step.kind === 'node') ? (
            <code className="rounded bg-stone-100 px-1.5 py-0.5 font-mono text-xs text-stone-700">{step.name}</code>
          ) : null}
          {step.durationMs !== null ? <span className="text-xs text-stone-600">{fmtMs(step.durationMs)}</span> : null}
        </div>
        {step.reason && step.kind === 'tool' && step.node === 'tools' ? (
          <p className="mt-0.5 text-sm text-stone-700">
            <span className="text-stone-600">Agent's note: </span>
            {step.reason}
          </p>
        ) : null}
        {step.summary ? <p className="mt-0.5 text-sm text-stone-700">{step.summary}</p> : null}
        {step.details.length ? (
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-stone-700">
            {step.details.map((d, i) => (
              <li key={i}>{d}</li>
            ))}
          </ul>
        ) : null}
      </div>
    </li>
  )
}

function useElapsed(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (startedAt === null) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [startedAt])
  return startedAt === null ? 0 : Math.max(0, now - startedAt)
}

export function TracePanel({ trace, running, startedAt }: { trace: TraceState; running: boolean; startedAt: number | null }) {
  const [open, setOpen] = useState(true)
  const [wasRunning, setWasRunning] = useState(running)
  const listId = useId()
  const elapsed = useElapsed(running ? startedAt : null)

  // Expand while a run streams; fold away once it finishes (the user can reopen it).
  if (running !== wasRunning) {
    setWasRunning(running)
    setOpen(running)
  }

  const visible = trace.steps
  const tools = visible.filter((s) => s.kind === 'tool').length
  if (!running && visible.length === 0) return null

  return (
    <section aria-labelledby={`${listId}-title`} className="rounded-2xl border border-stone-200 bg-white shadow-sm">
      <div className="flex items-center justify-between gap-3 p-4 sm:px-5">
        <div className="min-w-0">
          <h2 id={`${listId}-title`} className="text-base font-semibold text-stone-900">
            How I planned this
          </h2>
          <p className="text-sm text-stone-600">
            {running
              ? `Planning… ${Math.floor(elapsed / 1000)} s`
              : `${visible.length} steps, ${tools} tool ${tools === 1 ? 'call' : 'calls'}`}
          </p>
        </div>
        {!running ? (
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-sm font-medium text-orange-800 hover:bg-orange-50"
            aria-expanded={open}
            aria-controls={listId}
            onClick={() => setOpen((o) => !o)}
          >
            {open ? 'Hide steps' : 'Show steps'}
            <Icon name="chevronDown" className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`} />
          </button>
        ) : null}
      </div>

      {running && trace.degraded.length ? (
        <div className="px-4 pb-3 sm:px-5">
          <DegradationNotices items={trace.degraded} />
        </div>
      ) : null}

      <div id={listId} hidden={!open} className="border-t border-stone-100 px-4 pb-3 sm:px-5">
        {/* Not a live region: milestones are announced by RunStatus, not every step (D-P2-17). */}
        <ol className="divide-y divide-stone-100">
          {visible.map((s) => (
            <Step key={s.key} step={s} />
          ))}
        </ol>
        {running && visible.length === 0 ? <p className="py-3 text-sm text-stone-600">Connecting to the planner…</p> : null}
      </div>
    </section>
  )
}
