import { expect, type Page, type Response } from '@playwright/test'

export const BRIEF_JSON = {
  city: 'Bangalore',
  budget: 2000,
  available_time: '4 hours',
  mood: 'tired but wants to do something fun',
  interests: ['food', 'music', 'walks'],
  constraints: ['vegetarian', 'avoid crowded places'],
}

export const CLOCK = /\b\d{1,2}:\d{2} (AM|PM)/

/** Fail the test if any JavaScript dialog opens (an executed XSS payload would call alert()). */
export function forbidDialogs(page: Page): string[] {
  const dialogs: string[] = []
  page.on('dialog', async (d) => {
    dialogs.push(d.message())
    await d.dismiss()
  })
  return dialogs
}

/** Collect uncaught page errors and console errors. */
export function collectErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`)
  })
  return errors
}

export function description(page: Page) {
  return page.getByLabel('Describe your Saturday')
}

export function planButton(page: Page) {
  return page.getByRole('button', { name: 'Plan my Saturday' })
}

export function planHeading(page: Page) {
  return page.getByRole('heading', { level: 2, name: /^Your Saturday in / })
}

/** Wait for the SSE POST to finish and return its parsed result event. */
export async function resultFrom(response: Response): Promise<Record<string, unknown>> {
  const body = await response.text()
  const frames = body.split('\n\n').filter((f) => f.startsWith('data: '))
  const events = frames.map((f) => JSON.parse(f.slice(6)) as Record<string, unknown>)
  const result = events.reverse().find((e) => e.type === 'result')
  expect(result, 'stream has a result event').toBeTruthy()
  return (result as { data: Record<string, unknown> }).data
}

export function isPlanPost(r: Response): boolean {
  return r.request().method() === 'POST' && /\/api\/plan(\/[^/]+\/resume)?$/.test(new URL(r.url()).pathname)
}

/** Submit the planner form with a given description and wait for the plan POST to finish. */
export async function submitDescription(page: Page, text: string): Promise<Record<string, unknown>> {
  await description(page).fill(text)
  const [response] = await Promise.all([page.waitForResponse(isPlanPost), planButton(page).click()])
  return resultFrom(response)
}

/** The document never scrolls horizontally. */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const widths = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
    viewport: window.innerWidth,
  }))
  expect(widths.doc, `document scrollWidth ${widths.doc} > viewport ${widths.viewport}`).toBeLessThanOrEqual(
    widths.viewport,
  )
  expect(widths.body).toBeLessThanOrEqual(widths.viewport)
}

/** Press Tab until `predicate` holds for the focused element (keyboard reachability), at most `max` times. */
export async function tabUntil(
  page: Page,
  predicate: (el: { tag: string; text: string; label: string | null; role: string | null }) => boolean,
  max = 80,
  reverse = false,
): Promise<void> {
  for (let i = 0; i < max; i += 1) {
    await page.keyboard.press(reverse ? 'Shift+Tab' : 'Tab')
    const el = await page.evaluate(() => {
      const a = document.activeElement as HTMLElement | null
      if (!a) return { tag: '', text: '', label: null, role: null }
      const labelled = a.id ? document.querySelector(`label[for="${CSS.escape(a.id)}"]`)?.textContent ?? null : null
      return {
        tag: a.tagName.toLowerCase(),
        text: (a.textContent ?? '').trim(),
        label: a.getAttribute('aria-label') ?? labelled,
        role: a.getAttribute('role'),
      }
    })
    if (predicate(el)) return
  }
  throw new Error('element not reachable with Tab')
}
