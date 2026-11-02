import type { EvalSummary, FeedbackRating, PlanResult } from '../types'
import { ApiError, apiErrorFromResponse, normalizePlanResult } from './errors'

export { ApiError }

/** Backend limits (backend/app/api/routes.py). */
export const FEEDBACK_REASON_MAX = 300
export const ANSWER_MAX = 200

async function getJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const res = await fetch(path, { headers: { Accept: 'application/json' }, signal })
  if (!res.ok) throw await apiErrorFromResponse(res)
  return res.json()
}

/** GET /api/plans/{share_id}. Throws ApiError(404) for unknown or expired links. */
export async function getSharedPlan(shareId: string, signal?: AbortSignal): Promise<PlanResult> {
  const raw = await getJson(`/api/plans/${encodeURIComponent(shareId)}`, signal)
  const result = normalizePlanResult(raw)
  if (!result) throw new ApiError(500, 'This plan could not be read.')
  return result
}

/** POST /api/plans/{share_id}/feedback. */
export async function sendFeedback(shareId: string, rating: FeedbackRating, reason?: string): Promise<void> {
  const body: { rating: FeedbackRating; reason?: string } = { rating }
  const trimmed = reason?.trim()
  if (trimmed) body.reason = trimmed.slice(0, FEEDBACK_REASON_MAX)
  const res = await fetch(`/api/plans/${encodeURIComponent(shareId)}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await apiErrorFromResponse(res)
}

/** GET /api/evals/summary. The shape is rendered defensively; fields may be null or missing. */
export async function getEvalsSummary(signal?: AbortSignal): Promise<EvalSummary> {
  const raw = await getJson('/api/evals/summary', signal)
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { rows: [] }
  return raw as EvalSummary
}
