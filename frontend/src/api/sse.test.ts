// @vitest-environment node
import { describe, expect, it, vi } from 'vitest'
import complete from '../test/fixtures/plan_complete.json'
import traceEvents from '../test/fixtures/trace_events.json'
import type { TraceEvent } from '../types'
import { ApiError, StreamError } from './errors'
import { SseParser, parseTraceEvent, postPlan, readSseStream, resumePlan, type SseMessage } from './sse'

const encoder = new TextEncoder()

function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(c)
      controller.close()
    },
  })
}

async function collect(chunks: Uint8Array[]): Promise<SseMessage[]> {
  const out: SseMessage[] = []
  await readSseStream(streamOf(chunks), (m) => out.push(m))
  return out
}

/** A realistic stream: comments, heartbeats, an event: field, multi-line data, multi-byte UTF-8 and the result. */
function sampleStream(eol = '\n'): string {
  const ev = traceEvents as unknown as TraceEvent[]
  const lines = [
    ': connected',
    '',
    `data: ${JSON.stringify(ev[0])}`,
    '',
    'event: trace',
    `data: ${JSON.stringify(ev[2])}`, // summary contains "₹2,000"
    '',
    ': heartbeat',
    '',
    'data: {"type":"heartbeat","run_id":"r_test_0001",',
    'data: "seq":0,"ts":"2026-11-05T04:30:01Z"}',
    '',
    `data: ${JSON.stringify(ev[ev.length - 1])}`,
    '',
    '',
  ]
  return lines.join(eol)
}

describe('SseParser', () => {
  it('parses the sample stream into messages', async () => {
    const msgs = await collect([encoder.encode(sampleStream())])
    expect(msgs).toHaveLength(4)
    expect(msgs[1].event).toBe('trace')
    expect(msgs[0].event).toBe('message')
    expect(JSON.parse(msgs[1].data).summary).toBe('Bangalore, ₹2,000, 4 h, low energy')
    // multi-line data is joined with \n and is still valid JSON
    expect(msgs[2].data).toBe('{"type":"heartbeat","run_id":"r_test_0001",\n"seq":0,"ts":"2026-11-05T04:30:01Z"}')
    expect(JSON.parse(msgs[3].data).type).toBe('result')
  })

  for (const [name, eol] of [
    ['LF', '\n'],
    ['CRLF', '\r\n'],
    ['CR', '\r'],
  ] as const) {
    it(`gives identical results when split at every byte offset (${name})`, async () => {
      const bytes = encoder.encode(sampleStream(eol))
      const expected = await collect([bytes])
      expect(expected).toHaveLength(4)
      for (let i = 0; i <= bytes.length; i += 1) {
        const got = await collect([bytes.slice(0, i), bytes.slice(i)])
        expect(got, `split at byte ${i}`).toEqual(expected)
      }
    })
  }

  it('gives identical results when fed one byte at a time (CRLF, multi-byte characters)', async () => {
    const bytes = encoder.encode(sampleStream('\r\n'))
    const expected = await collect([bytes])
    const single = await collect(Array.from(bytes, (b) => new Uint8Array([b])))
    expect(single).toEqual(expected)
  })

  it('ignores ": keep-alive" comment lines between events (backend keep-alives)', async () => {
    const body =
      'data: {"type":"run_started","run_id":"r","seq":1,"ts":"2026-11-05T04:30:00Z"}\n\n: keep-alive\n\n: keep-alive\n\n' +
      'data: {"type":"result","run_id":"r","seq":2,"ts":"2026-11-05T04:30:01Z","data":' +
      JSON.stringify(complete) +
      '}\n\n'
    const msgs = await collect([encoder.encode(body)])
    expect(msgs.map((m) => JSON.parse(m.data).type)).toEqual(['run_started', 'result'])
    const seen: string[] = []
    const result = await postPlan({}, (e) => seen.push(e.type), undefined, { fetchImpl: async () => sseResponse(body) })
    expect(result.status).toBe('complete')
    expect(seen).toEqual(['run_started', 'result'])
  })

  it('ignores comments, unknown fields, lines without a colon and retry', () => {
    const got: SseMessage[] = []
    const p = new SseParser((m) => got.push(m))
    p.push(': just a comment\n\nthis line has no colon\nretry: 1000\nfoo: bar\ndata: ok\n\n')
    p.end()
    expect(got).toEqual([{ event: 'message', data: 'ok', id: null }])
  })

  it('does not dispatch events without data and handles a single space after the colon only', () => {
    const got: SseMessage[] = []
    const p = new SseParser((m) => got.push(m))
    p.push('event: lonely\n\ndata:  two spaces\ndata:none\n\n')
    p.end()
    expect(got).toEqual([{ event: 'message', data: ' two spaces\nnone', id: null }])
  })

  it('tracks id and strips a leading BOM', () => {
    const got: SseMessage[] = []
    const p = new SseParser((m) => got.push(m))
    p.push('\uFEFFid: 7\ndata: x\n\n')
    p.end()
    expect(got).toEqual([{ event: 'message', data: 'x', id: '7' }])
  })

  it('dispatches a final event that lacks the trailing blank line', () => {
    const got: SseMessage[] = []
    const p = new SseParser((m) => got.push(m))
    p.push('data: last')
    p.end()
    expect(got.map((m) => m.data)).toEqual(['last'])
  })
})

describe('parseTraceEvent', () => {
  it('rejects malformed JSON, non-objects and unknown types', () => {
    expect(parseTraceEvent('{not json')).toBeNull()
    expect(parseTraceEvent('[1,2]')).toBeNull()
    expect(parseTraceEvent('"text"')).toBeNull()
    expect(parseTraceEvent('{"type":"bogus","run_id":"r","seq":1,"ts":"x"}')).toBeNull()
    expect(parseTraceEvent('{"run_id":"r"}')).toBeNull()
  })

  it('accepts a valid event', () => {
    const ev = parseTraceEvent(JSON.stringify((traceEvents as unknown as TraceEvent[])[4]))
    expect(ev?.type).toBe('tool_completed')
    expect(ev?.tool).toBe('search_places')
    expect(ev?.duration_ms).toBe(612)
  })
})

function sseResponse(body: string, init: ResponseInit = {}): Response {
  const bytes = encoder.encode(body)
  // deliver in awkward 7-byte chunks
  const chunks: Uint8Array[] = []
  for (let i = 0; i < bytes.length; i += 7) chunks.push(bytes.slice(i, i + 7))
  return new Response(streamOf(chunks), {
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
    ...init,
  })
}

function fullStream(): string {
  const events = traceEvents as unknown as TraceEvent[]
  return (
    ': hello\n\n' +
    events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(': heartbeat\n\n') +
    'data: {malformed\n\n'
  )
}

describe('postPlan', () => {
  it('streams every valid event in order and resolves with the result', async () => {
    const fetchImpl = vi.fn(async () => sseResponse(fullStream()))
    const seen: string[] = []
    const result = await postPlan({ city: 'Bangalore' }, (e) => seen.push(e.type), undefined, { fetchImpl })
    expect(result.status).toBe('complete')
    expect(result.plan?.stops).toHaveLength(3)
    expect(result.share_id).toBe((complete as { share_id: string }).share_id)
    expect(seen).toEqual((traceEvents as unknown as TraceEvent[]).map((e) => e.type))

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/plan')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>).Accept).toBe('text/event-stream')
    expect(JSON.parse(init.body as string)).toEqual({ city: 'Bangalore' })
  })

  it('throws ApiError with retryAfter on 429', async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: 'Too many plans from this address.', status: 'rejected' }), {
          status: 429,
          headers: { 'content-type': 'application/json', 'Retry-After': '42' },
        }),
    )
    const err = await postPlan({}, () => {}, undefined, { fetchImpl }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(429)
    expect((err as ApiError).retryAfter).toBe(42)
    expect((err as ApiError).detail).toBe('Too many plans from this address.')
  })

  it('formats a list detail on 400 and ignores non-JSON bodies', async () => {
    const list = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: [{ loc: ['body', 'budget'], msg: 'must be >= 0' }], status: 'rejected' }), {
          status: 400,
        }),
    )
    const e1 = (await postPlan({}, () => {}, undefined, { fetchImpl: list }).catch((e: unknown) => e)) as ApiError
    expect(e1.detail).toBe('budget: must be >= 0')

    const html = vi.fn(async () => new Response('<html>bad gateway</html>', { status: 502 }))
    const e2 = (await postPlan({}, () => {}, undefined, { fetchImpl: html }).catch((e: unknown) => e)) as ApiError
    expect(e2.status).toBe(502)
    expect(e2.detail).not.toContain('<html>')
  })

  it('joins a 400 detail given as a list of strings (backend validation format)', async () => {
    const fetchImpl = async () =>
      new Response(JSON.stringify({ status: 'rejected', detail: ['budget: Input should be >= 0', 'city: too long'] }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      })
    const err = (await postPlan({}, () => {}, undefined, { fetchImpl }).catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.detail).toBe('budget: Input should be >= 0; city: too long')
    expect(err.retryAfter).toBeNull()
  })

  it('decodes HTML-escaped (\\u003c) frames like the backend sends and resolves a 200 "rejected" result', async () => {
    const result = { ...complete, status: 'rejected', plan: null, message: 'No <script> & friends' }
    const json = JSON.stringify({ type: 'result', run_id: 'r1', seq: 1, ts: '2026-11-05T04:30:00Z', data: result })
      .replace(/</g, '\\u003c')
      .replace(/>/g, '\\u003e')
      .replace(/&/g, '\\u0026')
    expect(json).not.toMatch(/[<>&]/)
    const got = await postPlan({}, () => {}, undefined, { fetchImpl: async () => sseResponse(`data: ${json}\n\n`) })
    expect(got.status).toBe('rejected')
    expect(got.message).toBe('No <script> & friends')
  })

  it('throws StreamError when the stream ends without a result, using the error event text', async () => {
    const body =
      'data: {"type":"run_started","run_id":"r1","seq":1,"ts":"2026-11-05T04:30:00Z"}\n\n' +
      'data: {"type":"error","run_id":"r1","seq":2,"ts":"2026-11-05T04:30:01Z","summary":"Planner failed, reference r1"}\n\n'
    const err = await postPlan({}, () => {}, undefined, { fetchImpl: async () => sseResponse(body) }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(StreamError)
    expect((err as StreamError).message).toBe('Planner failed, reference r1')
    expect((err as StreamError).runId).toBe('r1')

    const empty = await postPlan({}, () => {}, undefined, { fetchImpl: async () => sseResponse(': only comments\n\n') }).catch(
      (e: unknown) => e,
    )
    expect(empty).toBeInstanceOf(StreamError)
  })

  it('accepts a plain JSON response', async () => {
    const fetchImpl = async () =>
      new Response(JSON.stringify(complete), { status: 200, headers: { 'content-type': 'application/json' } })
    const result = await postPlan({}, () => {}, undefined, { fetchImpl })
    expect(result.status).toBe('complete')
  })
})

describe('resumePlan', () => {
  it('posts the answers to the resume endpoint', async () => {
    const fetchImpl = vi.fn(async () => sseResponse(fullStream()))
    const result = await resumePlan('th/1', { city: 'Bangalore' }, () => {}, undefined, { fetchImpl })
    expect(result.status).toBe('complete')
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/plan/th%2F1/resume')
    expect(JSON.parse(init.body as string)).toEqual({ answers: { city: 'Bangalore' } })
  })
})
