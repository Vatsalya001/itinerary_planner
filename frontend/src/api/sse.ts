import type { PlanRequest, PlanResult, TraceEvent, TraceType } from '../types'
import { ApiError, StreamError, apiErrorFromResponse, normalizePlanResult } from './errors'

export { ApiError, StreamError }

/** One dispatched Server-Sent Event. */
export interface SseMessage {
  event: string
  data: string
  id: string | null
}

/**
 * Incremental Server-Sent Events parser (WHATWG "event stream interpretation").
 *
 * Feed it decoded text in arbitrary chunks with `push`; it handles lines split across chunks, CRLF / CR / LF
 * line endings (including a CR at the end of one chunk and the LF at the start of the next), multi-line
 * `data:` fields, comments (`: heartbeat`), unknown fields and lines without a colon (ignored).
 * Call `end()` when the stream finishes; unlike the browser EventSource it also dispatches a final
 * event that was not followed by a blank line, so a truncated last `result` is not lost.
 */
export class SseParser {
  private buffer = ''
  private dataLines: string[] = []
  private eventType = ''
  private lastEventId: string | null = null
  private started = false
  private readonly onMessage: (msg: SseMessage) => void

  constructor(onMessage: (msg: SseMessage) => void) {
    this.onMessage = onMessage
  }

  push(chunk: string): void {
    if (!chunk) return
    let text = chunk
    if (!this.started) {
      this.started = true
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1)
    }
    this.buffer += text
    this.drain(false)
  }

  end(): void {
    this.drain(true)
    if (this.buffer.length > 0) {
      this.processLine(this.buffer)
      this.buffer = ''
    }
    this.dispatch()
  }

  private drain(final: boolean): void {
    const buf = this.buffer
    let start = 0
    let i = 0
    while (i < buf.length) {
      const c = buf.charCodeAt(i)
      if (c === 10 /* \n */) {
        this.processLine(buf.slice(start, i))
        start = i + 1
      } else if (c === 13 /* \r */) {
        if (i + 1 === buf.length && !final) break // might be the first half of a CRLF split across chunks
        this.processLine(buf.slice(start, i))
        if (buf.charCodeAt(i + 1) === 10) i += 1
        start = i + 1
      }
      i += 1
    }
    this.buffer = buf.slice(start)
  }

  private processLine(line: string): void {
    if (line === '') {
      this.dispatch()
      return
    }
    if (line.charCodeAt(0) === 58 /* : */) return // comment, e.g. ": heartbeat"
    const colon = line.indexOf(':')
    let field: string
    let value: string
    if (colon === -1) {
      field = line
      value = ''
    } else {
      field = line.slice(0, colon)
      value = line.slice(colon + 1)
      if (value.charCodeAt(0) === 32 /* space */) value = value.slice(1)
    }
    switch (field) {
      case 'data':
        this.dataLines.push(value)
        break
      case 'event':
        this.eventType = value
        break
      case 'id':
        if (!value.includes('\u0000')) this.lastEventId = value
        break
      default:
        // "retry" and unknown fields are ignored; we never auto-reconnect a POST.
        break
    }
  }

  private dispatch(): void {
    if (this.dataLines.length === 0) {
      this.eventType = ''
      return
    }
    const msg: SseMessage = {
      event: this.eventType || 'message',
      data: this.dataLines.join('\n'),
      id: this.lastEventId,
    }
    this.dataLines = []
    this.eventType = ''
    this.onMessage(msg)
  }
}

/** Read a fetch body as SSE, decoding UTF-8 incrementally (multi-byte characters may be split across chunks). */
export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onMessage: (msg: SseMessage) => void,
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  const parser = new SseParser(onMessage)
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      if (value) parser.push(decoder.decode(value, { stream: true }))
    }
    parser.push(decoder.decode())
    parser.end()
  } finally {
    reader.releaseLock()
  }
}

const TRACE_TYPES: ReadonlySet<TraceType> = new Set<TraceType>([
  'run_started',
  'node_started',
  'node_completed',
  'tool_called',
  'tool_completed',
  'tool_failed',
  'validation',
  'degraded',
  'clarification',
  'result',
  'error',
  'heartbeat',
])

/** Parse an SSE data payload into a TraceEvent; returns null for malformed or unknown payloads. */
export function parseTraceEvent(data: string): TraceEvent | null {
  let raw: unknown
  try {
    raw = JSON.parse(data)
  } catch {
    return null
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const ev = raw as Record<string, unknown>
  if (typeof ev.type !== 'string' || !TRACE_TYPES.has(ev.type as TraceType)) return null
  return {
    ...(ev as unknown as TraceEvent),
    run_id: typeof ev.run_id === 'string' ? ev.run_id : '',
    seq: typeof ev.seq === 'number' ? ev.seq : 0,
    ts: typeof ev.ts === 'string' ? ev.ts : '',
    data: ev.data && typeof ev.data === 'object' && !Array.isArray(ev.data) ? (ev.data as Record<string, unknown>) : null,
  }
}

export type TraceListener = (event: TraceEvent) => void

export interface StreamOptions {
  /** Base URL prefix for the API (default: same origin). */
  baseUrl?: string
  fetchImpl?: typeof fetch
}

function errorText(ev: TraceEvent): string | null {
  if (ev.summary) return ev.summary
  const d = ev.data
  if (d) {
    for (const key of ['message', 'detail', 'error']) {
      const v = d[key]
      if (typeof v === 'string' && v.trim()) return v
    }
  }
  return null
}

async function streamPlan(
  path: string,
  body: unknown,
  onEvent: TraceListener,
  signal: AbortSignal | undefined,
  options: StreamOptions,
): Promise<PlanResult> {
  const doFetch = options.fetchImpl ?? fetch
  const res = await doFetch(`${options.baseUrl ?? ''}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) throw await apiErrorFromResponse(res)

  const contentType = res.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    // The server answered without streaming (e.g. an early rejection); accept it.
    const result = normalizePlanResult(await res.json())
    if (!result) throw new StreamError('The planner sent a response we could not read.')
    return result
  }
  if (!res.body) throw new StreamError('The planner sent an empty response.')

  // Mutated from the stream callback; a holder object keeps TypeScript's narrowing honest.
  const outcome: { result: PlanResult | null; error: string | null; runId: string | null } = {
    result: null,
    error: null,
    runId: null,
  }

  await readSseStream(res.body, (msg) => {
    const ev = parseTraceEvent(msg.data)
    if (!ev) return // malformed payloads are ignored
    if (ev.run_id) outcome.runId = ev.run_id
    if (ev.type === 'result') {
      const parsed = normalizePlanResult(ev.data)
      if (parsed) outcome.result = parsed
    } else if (ev.type === 'error') {
      outcome.error = errorText(ev) ?? 'The planner hit an error.'
    }
    try {
      onEvent(ev)
    } catch (err) {
      // A rendering bug in a listener must not kill the run.
      console.error('trace listener failed', err)
    }
  })

  if (outcome.result) return outcome.result
  throw new StreamError(
    outcome.error ?? 'The connection closed before the plan was ready. Please try again.',
    outcome.runId,
  )
}

/** POST /api/plan as SSE. Resolves with the final PlanResult; throws ApiError (HTTP) or StreamError. */
export function postPlan(
  request: PlanRequest,
  onEvent: TraceListener,
  signal?: AbortSignal,
  options: StreamOptions = {},
): Promise<PlanResult> {
  return streamPlan('/api/plan', request, onEvent, signal, options)
}

/** POST /api/plan/{thread_id}/resume with clarification answers, as SSE. */
export function resumePlan(
  threadId: string,
  answers: Record<string, string>,
  onEvent: TraceListener,
  signal?: AbortSignal,
  options: StreamOptions = {},
): Promise<PlanResult> {
  return streamPlan(`/api/plan/${encodeURIComponent(threadId)}/resume`, { answers }, onEvent, signal, options)
}
