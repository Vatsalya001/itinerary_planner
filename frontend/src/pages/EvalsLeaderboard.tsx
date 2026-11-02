import { useEffect, useState } from 'react'
import { ApiError, getEvalsSummary } from '../api/client'
import { isAbortError } from '../api/errors'
import { ErrorCard } from '../components/ErrorCard'
import { Badge } from '../components/ui'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { describeError } from '../lib/describeError'
import { fmtMs, fmtPercent, humanize } from '../lib/format'
import type { EvalRow, EvalSummary } from '../types'

type State = { kind: 'loading' } | { kind: 'ok'; summary: EvalSummary } | { kind: 'error'; error: unknown }

const GROUPS: Array<{ kind: string; title: string; note: string }> = [
  { kind: 'baseline', title: 'Baseline (no LLM)', note: 'Rule-based parser and the deterministic planner: the floor each model has to beat.' },
  { kind: 'api', title: 'Hosted API models', note: 'Candidates for the production model.' },
  { kind: 'local', title: 'Local models (Ollama)', note: 'Eval and offline development only; run with a longer deadline that is reported, not hidden.' },
]

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function fmtNum(v: unknown, digits = 2): string {
  const n = num(v)
  return n === null ? '—' : n.toFixed(digits)
}

function fmtCost(v: unknown): string {
  const n = num(v)
  if (n === null) return '—'
  return n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

function fmtInt(v: unknown): string {
  const n = num(v)
  return n === null ? '—' : String(Math.round(n))
}

const COLUMNS: Array<{ key: keyof EvalRow; label: string; title: string; render: (r: EvalRow) => string }> = [
  { key: 'runs', label: 'Runs', title: 'Number of runs (n)', render: (r) => fmtInt(r.runs) },
  { key: 'hard_pass_rate', label: 'Hard pass', title: 'Runs passing every applicable assertion', render: (r) => fmtPercent(num(r.hard_pass_rate)) },
  { key: 'status_accuracy', label: 'Status acc.', title: "Runs whose status is in the case's expected set", render: (r) => fmtPercent(num(r.status_accuracy)) },
  { key: 'agent_success', label: 'Agent success', title: 'Complete in agent mode on feasible cases (fallbacks do not count)', render: (r) => fmtPercent(num(r.agent_success)) },
  { key: 'first_submission_validity', label: '1st submit valid', title: 'submit_plan valid on the first attempt', render: (r) => fmtPercent(num(r.first_submission_validity)) },
  { key: 'tool_protocol_errors', label: 'Tool errors / run', title: 'Malformed calls, unknown tools or schema-invalid arguments per run', render: (r) => fmtNum(r.tool_protocol_errors) },
  { key: 'clarification_precision', label: 'Clarif. precision', title: 'Clarifications on blocking cases / all clarifications asked', render: (r) => fmtPercent(num(r.clarification_precision)) },
  { key: 'clarification_recall', label: 'Clarif. recall', title: 'Blocking cases that got a clarification / blocking cases', render: (r) => fmtPercent(num(r.clarification_recall)) },
  { key: 'judge_quality', label: 'Judge\u00a0/\u00a05', title: 'Mean 1–5 per rubric axis (fit, practicality, explanation and trade-offs, fun) from a fixed judge model; hard failures score 1', render: (r) => fmtNum(r.judge_quality, 1) },
  { key: 'latency_p50_ms', label: 'p50', title: 'Median end-to-end latency', render: (r) => fmtMs(num(r.latency_p50_ms)) },
  { key: 'latency_p95_ms', label: 'p95', title: '95th percentile end-to-end latency', render: (r) => fmtMs(num(r.latency_p95_ms)) },
  { key: 'cost_per_plan_usd', label: 'Cost / plan', title: 'Tokens × registry pricing per plan (estimate)', render: (r) => fmtCost(r.cost_per_plan_usd) },
]

function LeaderboardTable({ rows, caption }: { rows: EvalRow[]; caption: string }) {
  const sorted = [...rows].sort((a, b) => (num(b.hard_pass_rate) ?? -1) - (num(a.hard_pass_rate) ?? -1))
  return (
    <div role="region" aria-label={`${caption} (scrollable table)`} tabIndex={0} className="overflow-x-auto rounded-xl border border-stone-200 bg-white">
      <table className="w-full min-w-[52rem] border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-stone-50 text-left text-xs text-stone-700">
          <tr>
            <th scope="col" className="sticky left-0 bg-stone-50 px-3 py-2 font-semibold">
              Model
            </th>
            {COLUMNS.map((c) => (
              <th key={c.key} scope="col" title={c.title} className="px-2 py-2 text-right align-bottom font-semibold">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-stone-100">
          {sorted.map((r, i) => (
            <tr key={`${r.model ?? 'model'}-${i}`}>
              <th scope="row" className="sticky left-0 bg-white px-3 py-2 text-left font-mono text-xs font-medium whitespace-nowrap text-stone-900">
                {r.model || 'Unnamed model'}
              </th>
              {COLUMNS.map((c) => (
                <td key={c.key} className="px-2 py-2 text-right text-stone-800 tabular-nums whitespace-nowrap">
                  {c.render(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function fmtCell(v: unknown, rates: boolean): string {
  if (typeof v === 'number' && Number.isFinite(v)) {
    if (rates) return fmtPercent(v)
    return Number.isInteger(v) ? String(v) : v.toFixed(2)
  }
  if (typeof v === 'string') return v
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  return '—'
}

/** Per-category breakdown. The shape isn't fixed, so only flat objects of scalars are tabulated. */
function Categories({ categories }: { categories: Record<string, unknown> }) {
  const entries = Object.entries(categories).filter(
    ([, v]) => v !== null && typeof v === 'object' && !Array.isArray(v),
  ) as Array<[string, Record<string, unknown>]>
  if (!entries.length) return null
  const columns = [...new Set(entries.flatMap(([, v]) => Object.keys(v).filter((k) => typeof v[k] !== 'object' || v[k] === null)))]
  if (!columns.length) return null
  // When every number is within 0..1 the table holds rates (1 means 100%, not one run).
  const numbers = entries.flatMap(([, v]) => Object.values(v)).filter((x): x is number => typeof x === 'number')
  const rates = numbers.length > 0 && numbers.every((n) => n >= 0 && n <= 1)
  return (
    <section aria-labelledby="categories-title" className="space-y-2">
      <h2 id="categories-title" className="text-lg font-semibold text-stone-900">
        By category
      </h2>
      <div role="region" aria-label="Results by category" tabIndex={0} className="overflow-x-auto rounded-xl border border-stone-200 bg-white">
        <table className="w-full border-collapse text-sm">
          <thead className="bg-stone-50 text-left text-xs text-stone-700">
            <tr>
              <th scope="col" className="px-3 py-2 font-semibold">
                Category
              </th>
              {columns.map((c) => (
                <th key={c} scope="col" className="px-3 py-2 text-right font-semibold whitespace-nowrap">
                  {c.includes(':') ? c : humanize(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-stone-100">
            {entries.map(([name, v]) => (
              <tr key={name}>
                <th scope="row" className="px-3 py-2 text-left font-medium text-stone-900">
                  {humanize(name)}
                </th>
                {columns.map((c) => (
                  <td key={c} className="px-3 py-2 text-right text-stone-800 tabular-nums whitespace-nowrap">
                    {fmtCell(v[c], rates)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export default function EvalsLeaderboard() {
  useDocumentTitle('Model evals · Perfect Saturday Planner')
  const [state, setState] = useState<State>({ kind: 'loading' })

  useEffect(() => {
    const ac = new AbortController()
    getEvalsSummary(ac.signal)
      .then((summary) => setState({ kind: 'ok', summary }))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setState({ kind: 'error', error })
      })
    return () => ac.abort()
  }, [])

  const rows = state.kind === 'ok' && Array.isArray(state.summary.rows) ? state.summary.rows.filter((r) => r && typeof r === 'object') : []
  const known = new Set(GROUPS.map((g) => g.kind))
  const other = rows.filter((r) => !known.has(String(r.kind ?? '')))

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-stone-900">Model evals</h1>
        <p className="max-w-3xl text-stone-700">
          Every model plans the same cases against a frozen world (fixed places, weather, events, travel and clock), so
          rows are comparable. The validator stops any invalid plan from being shown regardless of model; models differ
          in how often they reach a valid plan without falling back, how good it is, how fast, and at what cost.
        </p>
      </div>

      {state.kind === 'loading' ? (
        <p role="status" className="text-stone-700">
          Loading results…
        </p>
      ) : null}

      {state.kind === 'error' ? (
        state.error instanceof ApiError && state.error.status === 404 ? (
          <ErrorCard title="No results published yet" message="The eval report hasn't been generated for this deployment." />
        ) : (
          <ErrorCard {...describeError(state.error)} />
        )
      ) : null}

      {state.kind === 'ok' ? (
        <>
          <p className="flex flex-wrap gap-2 text-sm text-stone-700">
            {state.summary.generated_at ? <Badge>{`Generated ${state.summary.generated_at}`}</Badge> : null}
            {state.summary.git_sha ? <Badge>{`Commit ${String(state.summary.git_sha).slice(0, 12)}`}</Badge> : null}
            <Badge>{`${rows.length} ${rows.length === 1 ? 'model' : 'models'}`}</Badge>
          </p>

          {rows.length === 0 ? (
            <p className="rounded-xl border border-stone-200 bg-white p-4 text-stone-700">No eval runs have been published yet.</p>
          ) : null}

          {GROUPS.map((g) => {
            const groupRows = rows.filter((r) => r.kind === g.kind)
            if (!groupRows.length) return null
            return (
              <section key={g.kind} aria-labelledby={`group-${g.kind}`} className="space-y-2">
                <h2 id={`group-${g.kind}`} className="text-lg font-semibold text-stone-900">
                  {g.title}
                </h2>
                <p className="text-sm text-stone-600">{g.note}</p>
                <LeaderboardTable rows={groupRows} caption={g.title} />
              </section>
            )
          })}
          {other.length ? (
            <section aria-labelledby="group-other" className="space-y-2">
              <h2 id="group-other" className="text-lg font-semibold text-stone-900">
                Other
              </h2>
              <LeaderboardTable rows={other} caption="Other models" />
            </section>
          ) : null}

          {state.summary.categories && typeof state.summary.categories === 'object' ? (
            <Categories categories={state.summary.categories} />
          ) : null}

          <details className="rounded-xl border border-stone-200 bg-white p-4 text-sm text-stone-800">
            <summary className="cursor-pointer font-semibold text-stone-900">What the columns mean</summary>
            <dl className="mt-3 grid gap-x-4 gap-y-2 sm:grid-cols-[auto_1fr]">
              {COLUMNS.map((c) => (
                <div key={c.key} className="contents">
                  <dt className="font-medium text-stone-900">{c.label}</dt>
                  <dd className="text-stone-700">{c.title}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-xs text-stone-600">
              “—” means the metric doesn't apply or wasn't measured. Cost is an estimate; local rows cost $0 plus wall time.
            </p>
          </details>
        </>
      ) : null}
    </div>
  )
}
