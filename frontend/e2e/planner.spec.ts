/**
 * Phase 2 browser verification: the built UI served by the real backend (fake:agent model, frozen world).
 * See playwright.config.ts for how the server is started.
 */
import { expect, test } from '@playwright/test'
import {
  BRIEF_JSON,
  CLOCK,
  collectErrors,
  description,
  expectNoHorizontalOverflow,
  forbidDialogs,
  isPlanPost,
  planButton,
  planHeading,
  resultFrom,
  submitDescription,
  tabUntil,
} from './helpers'

const VAGUE = 'plan something fun for saturday'
const IMPOSSIBLE_BUDGET =
  "I'm in Bangalore with ₹100 and 4 hours this Saturday. Chill mood. I like food and music. I'm vegetarian and need wheelchair access."
const IMPOSSIBLE_CONSTRAINTS =
  "I'm in Bangalore with ₹2000 and 4 hours this Saturday. I'm tired. I'm vegan and jain, need wheelchair access and want to avoid crowded places."

test.describe('planner, brief example', () => {
  test('the prefilled brief plans end to end with timeline cards, costs, badges, checks and a trace', async ({ page }) => {
    const errors = collectErrors(page)
    const dialogs = forbidDialogs(page)
    await page.goto('/')
    await expect(description(page)).toHaveValue(/Bangalore/)

    const [response] = await Promise.all([page.waitForResponse(isPlanPost), planButton(page).click()])
    expect(response.headers()['content-type']).toContain('text/event-stream')
    const result = await resultFrom(response)
    expect(result.status).toBe('complete')
    expect(result.mode).toBe('agent')

    await expect(planHeading(page)).toBeVisible()
    await expect(planHeading(page)).toBeFocused() // focus moves to the outcome

    const cards = page.locator('section[aria-labelledby="plan-title"] article')
    const count = await cards.count()
    expect(count).toBeGreaterThanOrEqual(2)
    for (let i = 0; i < count; i += 1) {
      const card = cards.nth(i)
      await expect(card).toContainText(CLOCK) // clock times, not relative offsets
      await expect(card).toContainText(/₹|free/i) // a cost or free
      await expect(card.getByText('Test data')).toBeVisible() // source badge
      const maps = card.getByRole('link', { name: /Open in Google Maps/ })
      await expect(maps).toHaveAttribute('href', /^https:\/\/www\.google\.com\/maps\//)
      await expect(maps).toHaveAttribute('rel', /noopener/)
    }
    // Legs between stops are labelled estimates.
    await expect(page.getByText('estimate', { exact: true }).first()).toBeVisible()

    // Summary: worst case against the budget and the time window.
    await expect(page.getByText('Worst-case cost per person')).toBeVisible()
    await expect(page.getByText(/of ₹2,000/)).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Checks' })).toBeVisible()

    // Trace: folded after the run, with real tool calls and the agent's notes.
    const traceTitle = page.getByRole('heading', { name: 'How I planned this' })
    await expect(traceTitle).toBeVisible()
    await expect(page.getByText(/\d+ steps, \d+ tool calls/)).toBeVisible()
    await page.getByRole('button', { name: 'Show steps' }).click()
    await expect(page.locator('code', { hasText: 'search_places' }).first()).toBeVisible()
    await expect(page.locator('code', { hasText: 'check_plan' }).first()).toBeVisible()
    await expect(page.getByText("Agent's note:").first()).toBeVisible()
    await expect(page.getByText('Plan passed the checks')).toBeVisible()

    // "What I understood" chips.
    await expect(page.getByRole('heading', { name: 'What I understood' })).toBeVisible()
    await expect(page.locator('section[aria-labelledby="understood-title"]').getByText('Low energy')).toBeVisible()

    expect(dialogs).toEqual([])
    expect(errors).toEqual([])
  })

  test("the brief's JSON (form fields, no description) is sent as-is and plans", async ({ page }) => {
    await page.goto('/')
    await description(page).fill('')
    await page.getByRole('button', { name: 'Fine-tune the details' }).click()
    await expect(page.getByLabel('City')).toHaveValue('Bangalore')
    const [request] = await Promise.all([
      page.waitForRequest((r) => r.method() === 'POST' && r.url().endsWith('/api/plan')),
      planButton(page).click(),
    ])
    const body = request.postDataJSON() as Record<string, unknown>
    expect(body.city).toBe(BRIEF_JSON.city)
    expect(body.budget).toBe(BRIEF_JSON.budget)
    expect(body.interests).toEqual(BRIEF_JSON.interests)
    expect(body.constraints).toEqual(BRIEF_JSON.constraints)
    expect(body.free_text).toBeUndefined()
    await expect(planHeading(page)).toBeVisible()
  })
})

test.describe('clarification', () => {
  test('a vague request shows the clarification card and answering resumes the same thread into a plan', async ({ page }) => {
    await page.goto('/')
    const first = await submitDescription(page, VAGUE)
    expect(first.status).toBe('needs_clarification')
    const threadId = first.thread_id as string
    expect(threadId).toBeTruthy()

    await expect(page.getByRole('heading', { name: 'Two quick questions' })).toBeFocused()
    await expect(page.locator('legend', { hasText: 'Which city are you spending Saturday in?' })).toBeVisible()
    const submit = page.getByRole('button', { name: 'Continue planning' })
    await expect(submit).toBeDisabled() // every question needs an answer

    await page.getByRole('button', { name: 'Bangalore' }).click()
    await page.getByRole('button', { name: '3 hours, ₹1000' }).click()
    const [response] = await Promise.all([page.waitForResponse(isPlanPost), submit.click()])
    expect(new URL(response.url()).pathname).toBe(`/api/plan/${threadId}/resume`)
    const result = await resultFrom(response)
    expect(result.status).toBe('complete')

    await expect(planHeading(page)).toBeVisible()
    await expect(page.getByText(/of ₹1,000/)).toBeVisible()
    await page.getByRole('button', { name: 'Show steps' }).click()
    await expect(page.getByText('Picked up again with your answer')).toBeVisible()
  })

  test('typed answers work too, and an expired thread gives a clear message', async ({ page, request }) => {
    await page.goto('/')
    const first = await submitDescription(page, VAGUE)
    const threadId = first.thread_id as string
    await page.getByLabel('Or type your own answer').first().fill('Mumbai')
    await page.getByRole('button', { name: '4 hours, ₹2000' }).click()
    await Promise.all([page.waitForResponse(isPlanPost), page.getByRole('button', { name: 'Continue planning' }).click()])
    await expect(page.getByRole('heading', { name: 'Your Saturday in Mumbai' })).toBeVisible()

    // The thread is finished; answering again is rejected with a friendly message (API contract).
    const again = await request.post(`/api/plan/${threadId}/resume`, {
      data: { answers: { city: 'Bangalore' } },
      headers: { Accept: 'application/json' },
    })
    const body = await again.json()
    expect(body.status).toBe('rejected')
    expect(body.message).toMatch(/expired or was already answered/)
  })
})

test.describe('no feasible plan', () => {
  test('relaxation buttons resubmit and a raised budget produces a plan', async ({ page }) => {
    await page.goto('/')
    const first = await submitDescription(page, IMPOSSIBLE_BUDGET)
    expect(first.status).toBe('no_feasible_plan')
    await expect(page.getByRole('heading', { name: 'No plan fits everything' })).toBeFocused()
    const raise = page.getByRole('button', { name: /^Raise budget to ₹/ })
    await expect(raise).toBeVisible()

    const [req] = await Promise.all([
      page.waitForRequest((r) => r.method() === 'POST' && r.url().endsWith('/api/plan')),
      raise.click(),
    ])
    const body = req.postDataJSON() as Record<string, unknown>
    expect(typeof body.budget).toBe('number')
    expect(body.free_text).toBe(IMPOSSIBLE_BUDGET)
    await expect(planHeading(page)).toBeVisible()
  })

  test('"Drop <constraint>" actually drops it when the request was free text (D-P2-02)', async ({ page }) => {
    // Fixed: the patch is {"relaxed_constraints": [key]}, unioned into the request; the parser subtracts it.
    await page.goto('/')
    const first = await submitDescription(page, IMPOSSIBLE_CONSTRAINTS)
    expect(first.status).toBe('no_feasible_plan')
    const drop = page.getByRole('button', { name: /^Drop '.*wheelchair.*'$/ })
    await expect(drop).toBeVisible()
    const [response] = await Promise.all([page.waitForResponse(isPlanPost), drop.click()])
    const sent = response.request().postDataJSON() as Record<string, unknown>
    expect(sent.free_text).toBe(IMPOSSIBLE_CONSTRAINTS)
    expect(sent.relaxed_constraints).toEqual(['access:wheelchair'])
    const second = await resultFrom(response)
    const kept = ((second.preferences as { hard_constraints?: Array<{ kind: string; value: string }> })
      ?.hard_constraints ?? []).map((c) => `${c.kind}:${c.value}`)
    expect(kept).not.toContain('access:wheelchair')
  })
})

test.describe('share link', () => {
  test('the share page renders the same plan, takes feedback, and unknown links get a friendly 404', async ({ page }) => {
    await page.goto('/')
    const result = await submitDescription(page, (await description(page).inputValue()) || 'Bangalore, ₹2000, 4 hours')
    const shareId = result.share_id as string
    expect(shareId).toBeTruthy()
    const names = await page.locator('section[aria-labelledby="plan-title"] article h4').allTextContents()

    await page.goto(`/p/${shareId}`)
    await expect(planHeading(page)).toBeVisible()
    await expect(planHeading(page)).toBeFocused() // focus lands on the loaded plan, like the planner result
    await expect(page.locator('section[aria-labelledby="plan-title"] article h4')).toHaveText(names)
    await expect(page.getByText('Want one for yourself?')).toBeVisible()
    await page.getByRole('button', { name: 'Yes' }).click()
    await expect(page.getByText('Thanks for the feedback.')).toBeVisible()

    // The calendar confirmation appears only after the browser really received the file.
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('button', { name: 'Add to calendar' }).click(),
    ])
    expect(download.suggestedFilename()).toMatch(/^saturday-[a-z0-9-]+-\d{4}-\d{2}-\d{2}\.ics$/)
    await expect(page.getByText('Calendar file downloaded.')).toBeVisible()

    await page.goto('/p/doesnotexist123')
    await expect(page.getByRole('heading', { name: "This plan isn't available" })).toBeVisible()
    await expect(page.getByRole('heading', { name: "This plan isn't available" })).toBeFocused()
  })
})

test.describe('security', () => {
  test('XSS strings in the form render literally and never execute', async ({ page }) => {
    const dialogs = forbidDialogs(page)
    const errors = collectErrors(page)
    const payload = '<img src=x onerror=alert(1)> chill'
    await page.goto('/')
    await description(page).fill('')
    await page.getByRole('button', { name: 'Fine-tune the details' }).click()
    await page.getByLabel('Mood').fill(payload)
    await Promise.all([page.waitForResponse(isPlanPost), planButton(page).click()])
    await expect(planHeading(page)).toBeVisible()
    await expect(page.getByText(payload)).toBeVisible() // "What I understood" shows the mood verbatim
    await expect(page.locator('img[src="x"]')).toHaveCount(0)

    // A script tag in the city comes back inside a clarification question.
    await page.getByLabel('City').fill('Bangalore<script>alert(2)</script>')
    await Promise.all([page.waitForResponse(isPlanPost), planButton(page).click()])
    await expect(page.locator('legend', { hasText: 'Bangalore<script>alert(2)</script>' })).toBeVisible()
    await expect(page.locator('script:not([src])', { hasText: 'alert' })).toHaveCount(0)
    expect(dialogs).toEqual([])
    expect(errors).toEqual([])
  })

  test('a 429 from the API shows the friendly wait message', async ({ page }) => {
    await page.route('**/api/plan', (route) =>
      route.fulfill({
        status: 429,
        headers: { 'Content-Type': 'application/json', 'Retry-After': '256' },
        body: JSON.stringify({ status: 'rejected', detail: 'Too many plans; try again in 256 seconds' }),
      }),
    )
    await page.goto('/')
    await planButton(page).click()
    await expect(page.getByRole('heading', { name: 'The planner is busy' })).toBeVisible()
    await expect(page.getByText('Too many plans; try again in 256 seconds')).toBeVisible()
  })
})

test.describe('mobile 390 px', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('no horizontal overflow on the planner, plan, clarification, infeasible, share and info pages', async ({ page }) => {
    await page.goto('/')
    await expectNoHorizontalOverflow(page)
    const result = await submitDescription(page, (await description(page).inputValue()) || 'Bangalore')
    await expect(planHeading(page)).toBeVisible()
    await page.getByRole('button', { name: 'Show steps' }).click()
    await expectNoHorizontalOverflow(page)

    await submitDescription(page, VAGUE)
    await expect(page.getByRole('heading', { name: 'Two quick questions' })).toBeVisible()
    await expectNoHorizontalOverflow(page)

    await submitDescription(page, IMPOSSIBLE_CONSTRAINTS)
    await expect(page.getByRole('heading', { name: 'No plan fits everything' })).toBeVisible()
    await expectNoHorizontalOverflow(page)

    await page.goto(`/p/${result.share_id as string}`)
    await expect(planHeading(page)).toBeVisible()
    await expectNoHorizontalOverflow(page)

    for (const path of ['/how-it-works', '/evals', '/p/doesnotexist123']) {
      await page.goto(path)
      await page.waitForLoadState('networkidle')
      await expectNoHorizontalOverflow(page)
    }
  })
})

test.describe('keyboard only', () => {
  test('describe, plan, answer a clarification and reach the actions without a mouse', async ({ page }) => {
    await page.goto('/')
    // Skip link first.
    await page.keyboard.press('Tab')
    await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused()

    await tabUntil(page, (el) => el.tag === 'textarea')
    await page.keyboard.press('ControlOrMeta+A')
    await page.keyboard.type(VAGUE)
    await tabUntil(page, (el) => el.tag === 'button' && el.text === 'Plan my Saturday')
    const outline = await page.evaluate(() => getComputedStyle(document.activeElement as Element).outlineStyle)
    expect(outline).not.toBe('none') // visible focus
    const [first] = await Promise.all([page.waitForResponse(isPlanPost), page.keyboard.press('Enter')])
    expect((await resultFrom(first)).status).toBe('needs_clarification')
    await expect(page.getByRole('heading', { name: 'Two quick questions' })).toBeFocused()

    await tabUntil(page, (el) => el.tag === 'button' && el.text === 'Bangalore')
    await page.keyboard.press('Enter')
    await tabUntil(page, (el) => el.tag === 'button' && el.text === '3 hours, ₹1000')
    await page.keyboard.press('Space')
    await tabUntil(page, (el) => el.tag === 'button' && el.text === 'Continue planning')
    const [second] = await Promise.all([page.waitForResponse(isPlanPost), page.keyboard.press('Enter')])
    expect((await resultFrom(second)).status).toBe('complete')
    await expect(planHeading(page)).toBeFocused()

    await tabUntil(page, (el) => el.tag === 'button' && el.text.includes('Copy plan'))
    await tabUntil(page, (el) => el.tag === 'button' && el.text.includes('Yes'))
    await page.keyboard.press('Enter')
    await expect(page.getByText('Thanks for the feedback.')).toBeVisible()
    // The trace sits above the result: walk back to it.
    await tabUntil(page, (el) => el.tag === 'button' && el.text.includes('Show steps'), 120, true)
    await page.keyboard.press('Enter')
    await expect(page.locator('code', { hasText: 'search_places' }).first()).toBeVisible()
  })
})
