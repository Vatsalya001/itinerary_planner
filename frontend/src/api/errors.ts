import type { PlanResult, Status } from '../types'

/** An HTTP error from the API (400/404/413/429/5xx). `retryAfter` is in seconds when the server sent it. */
export class ApiError extends Error {
  override readonly name = 'ApiError'
  readonly status: number
  readonly detail: string
  readonly retryAfter: number | null

  constructor(status: number, detail: string, retryAfter: number | null = null) {
    super(detail)
    this.status = status
    this.detail = detail
    this.retryAfter = retryAfter
  }
}

/** The SSE stream ended without a result, or the server sent an `error` event. */
export class StreamError extends Error {
  override readonly name = 'StreamError'
  readonly runId: string | null

  constructor(message: string, runId: string | null = null) {
    super(message)
    this.runId = runId
  }
}

export function isAbortError(err: unknown): boolean {
  return (
    typeof err === 'object' &&
    err !== null &&
    'name' in err &&
    ((err as { name: unknown }).name === 'AbortError' || (err as { name: unknown }).name === 'TimeoutError')
  )
}

const DEFAULT_MESSAGES: Record<number, string> = {
  400: "The planner couldn't use that request. Please check the details and try again.",
  404: "We couldn't find that.",
  413: 'That request is too long. Please shorten the description.',
  422: "The planner couldn't use that request. Please check the details and try again.",
  429: 'Too many plans are being made right now.',
}

/** FastAPI `detail` may be a string or a list of validation errors ({loc, msg}). */
export function formatDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail.trim() || null
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        if (typeof d === 'string') return d
        if (d && typeof d === 'object') {
          const msg = typeof (d as { msg?: unknown }).msg === 'string' ? (d as { msg: string }).msg : null
          const loc = (d as { loc?: unknown }).loc
          const field = Array.isArray(loc) ? loc.filter((x) => x !== 'body').join('.') : ''
          if (msg) return field ? `${field}: ${msg}` : msg
        }
        return null
      })
      .filter((x): x is string => Boolean(x))
    return parts.length ? parts.join('; ') : null
  }
  return null
}

/** Parse a Retry-After header: delta-seconds or an HTTP date. Returns whole seconds (>= 0) or null. */
export function parseRetryAfter(value: string | null, now: number = Date.now()): number | null {
  if (!value) return null
  const trimmed = value.trim()
  if (/^\d+$/.test(trimmed)) return Number.parseInt(trimmed, 10)
  const at = Date.parse(trimmed)
  if (Number.isNaN(at)) return null
  return Math.max(0, Math.ceil((at - now) / 1000))
}

export async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  let detail: string | null = null
  try {
    const text = await res.text()
    if (text) {
      try {
        const body: unknown = JSON.parse(text)
        if (body && typeof body === 'object') {
          detail = formatDetail((body as { detail?: unknown }).detail)
        }
      } catch {
        // Not JSON; never show raw HTML/text bodies from proxies.
      }
    }
  } catch {
    // body unreadable
  }
  const fallback =
    DEFAULT_MESSAGES[res.status] ??
    (res.status >= 500 ? 'Something went wrong on our side. Please try again.' : `Request failed (${res.status}).`)
  return new ApiError(res.status, detail ?? fallback, parseRetryAfter(res.headers.get('Retry-After')))
}

const STATUSES: ReadonlySet<Status> = new Set([
  'complete',
  'fallback',
  'needs_clarification',
  'no_feasible_plan',
  'rejected',
])

function arr<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

/** Validate the minimum shape of a PlanResult and fill list defaults so the UI can render defensively. */
export function normalizePlanResult(raw: unknown): PlanResult | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if (typeof r.status !== 'string' || !STATUSES.has(r.status as Status)) return null
  return {
    ...(r as unknown as PlanResult),
    run_id: typeof r.run_id === 'string' ? r.run_id : '',
    checks: arr(r.checks),
    questions: arr(r.questions),
    violations: arr(r.violations),
    degradations: arr(r.degradations),
    assumptions: arr(r.assumptions),
    warnings: arr(r.warnings),
  }
}
