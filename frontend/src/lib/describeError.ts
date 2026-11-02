import { ApiError, StreamError, isAbortError, isTimeoutError } from '../api/errors'

export interface ErrorInfo {
  title: string
  message: string
  retryAfter: number | null
  runId: string | null
}

/** Turn anything thrown by the API layer into user-facing copy. Never shows stack traces. */
export function describeError(error: unknown): ErrorInfo {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return { title: 'The planner is busy', message: error.detail, retryAfter: error.retryAfter, runId: null }
    }
    if (error.status === 400 || error.status === 413 || error.status === 422) {
      return { title: "I can't plan that", message: error.detail, retryAfter: null, runId: null }
    }
    if (error.status === 404) {
      return { title: 'Not found', message: error.detail, retryAfter: null, runId: null }
    }
    return {
      title: 'Something went wrong',
      message: error.status >= 500 ? 'Something went wrong on our side. Please try again.' : error.detail,
      retryAfter: error.retryAfter,
      runId: null,
    }
  }
  if (error instanceof StreamError) {
    return { title: 'The plan was interrupted', message: error.message, retryAfter: null, runId: error.runId }
  }
  // An abort or timeout the app did not ask for (callers swallow only their own aborts).
  if (isTimeoutError(error)) {
    return {
      title: 'The plan was interrupted',
      message: 'The planner took too long to answer. Please try again.',
      retryAfter: null,
      runId: null,
    }
  }
  if (isAbortError(error)) {
    return {
      title: 'The plan was interrupted',
      message: 'The connection was cut off before the plan was ready. Please try again.',
      retryAfter: null,
      runId: null,
    }
  }
  if (error instanceof TypeError) {
    return {
      title: "Couldn't reach the planner",
      message: 'Check your connection and try again.',
      retryAfter: null,
      runId: null,
    }
  }
  return { title: 'Something went wrong', message: 'Please try again.', retryAfter: null, runId: null }
}
