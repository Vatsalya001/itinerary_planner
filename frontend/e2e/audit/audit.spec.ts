/**
 * Independent frontend audit, browser reproductions against the real backend (docs/verification/AUDIT_FRONTEND.md).
 *
 * Runs with the normal e2e server (fake:agent model, fixture world; see ../playwright.config.ts) or any server
 * started the same way (E2E_BASE_URL=...). Each confirmed bug asserts the CORRECT behaviour and is marked
 * `test.fail(...)` with its ID, so the suite stays green while the bug exists and flags the test once a fix makes
 * it pass. AUDIT_STRICT=1 runs them as plain tests to show each failure. Tests without `test.fail` are passing
 * regression guards for behaviour the audit checked and found correct.
 */
import { expect, test, type Page } from '@playwright/test'
import {
  description,
  expectNoHorizontalOverflow,
  forbidDialogs,
  isPlanPost,
  planButton,
  planHeading,
  resultFrom,
  submitDescription,
} from '../helpers'


const knownBug = (reason: string) => test.info().annotations.push({ type: 'regression', description: reason })

const BRIEF_TEXT =
  "I'm in Bangalore with ₹2000 and 4 hours this Saturday. I'm tired but want to do something fun. " +
  "I like food, music and walks. I'm vegetarian and want to avoid crowded places."

function understood(page: Page) {
  return page.locator('section[aria-labelledby="understood-title"]')
}

async function clickAndResult(page: Page, name: string | RegExp): Promise<Record<string, unknown>> {
  const [response] = await Promise.all([page.waitForResponse(isPlanPost), page.getByRole('button', { name }).click()])
  return resultFrom(response)
}

test.describe('audit: description vs. edited fields', () => {
  test('[F-01a] a chip edited earlier does not replace the interests of a rewritten description', async ({ page }) => {
    knownBug('F-01: dirty chips are always sent and the backend drops the interests written in the text')
    await page.goto('/')
    await page.getByRole('button', { name: 'Fine-tune the details' }).click()
    await page.getByRole('button', { name: 'Coffee', exact: true }).click()
    await page.getByRole('button', { name: 'Hide the details' }).click()
    const result = await submitDescription(
      page,
      "I'm in Bangalore with ₹2000 and 4 hours this Saturday. Chill mood. I love art galleries and museums.",
    )
    expect(result.status).toBe('complete')
    // Today: Food, Music, Walks, Coffee; nothing from the description.
    await expect(understood(page)).toContainText(/Art|Museums/, { timeout: 3000 })
  })

  test('[F-01b] a mood set by "Plan at a moderate pace" does not override a later description', async ({ page }) => {
    knownBug('F-01: the relaxation marks mood as edited and it is silently sent with every later plan')
    await page.goto('/')
    const first = await submitDescription(
      page,
      "I'm in Bangalore with ₹2000 and 4 hours this Saturday. I'm tired. I'm vegan and jain, need wheelchair access and want to avoid crowded places.",
    )
    expect(first.status).toBe('no_feasible_plan')
    const relaxed = await clickAndResult(page, 'Plan at a moderate pace')
    expect(relaxed.status).toBe('complete')
    const later = await submitDescription(
      page,
      "I'm in Bangalore with ₹2000 and 4 hours. I'm exhausted and need wheelchair access. I like museums.",
    )
    expect(later.status).toBe('complete')
    // Today: Medium energy and mood "feeling okay, happy with a moderate pace".
    await expect(understood(page)).toContainText('Low energy', { timeout: 3000 })
  })

  test('[F-02] unticking "Vegetarian" in the form drops the constraint the panel says it overrides', async ({ page }) => {
    knownBug('F-02: constraints are unioned with the text, so the toggle has no effect in description mode')
    await page.goto('/')
    await page.getByRole('button', { name: 'Fine-tune the details' }).click()
    await expect(page.getByText(/Fields you change here override what the planner reads/)).toBeVisible()
    await page.getByLabel('Vegetarian').uncheck()
    const result = await submitDescription(page, BRIEF_TEXT)
    expect(result.status).toBe('complete')
    await expect(understood(page)).not.toContainText('Vegetarian', { timeout: 3000 })
  })
})

test.describe('audit: clarification follow-ups', () => {
  test('[F-03] a relaxation after answering a clarification does not ask the same questions again', async ({ page }) => {
    knownBug('F-03: onRelax rebuilds the request from the pre-answer request (Planner.tsx lastRequest)')
    await page.goto('/')
    const first = await submitDescription(
      page,
      "I'm tired. I'm vegan and jain, need wheelchair access and want to avoid crowded places.",
    )
    expect(first.status).toBe('needs_clarification')
    await page.getByRole('button', { name: 'Bangalore' }).click()
    await page.getByRole('button', { name: '4 hours, ₹2000' }).click()
    const answered = await clickAndResult(page, 'Continue planning')
    expect(answered.status).toBe('no_feasible_plan')
    const relaxed = await clickAndResult(page, /^Drop /)
    // Today: needs_clarification, "Two quick questions" again (city and budget/time).
    expect(relaxed.status).not.toBe('needs_clarification')
  })

  test('[F-05] an unknown city typed as an answer is not reported as "No plan fits everything"', async ({ page }) => {
    knownBug('F-05: every no_feasible_plan renders InfeasibleCard, even without an infeasible report')
    await page.goto('/')
    const first = await submitDescription(page, 'plan something fun for saturday')
    expect(first.status).toBe('needs_clarification')
    await page.getByLabel('Or type your own answer').first().fill('Atlantisville')
    await page.getByRole('button', { name: '4 hours, ₹2000' }).click()
    const answered = await clickAndResult(page, 'Continue planning')
    expect(answered.status).toBe('no_feasible_plan')
    expect(answered.infeasible).toBeNull()
    await expect(page.getByText(/couldn't find a city called/)).toBeVisible()
    await expect(page.getByRole('heading', { name: 'No plan fits everything' })).toHaveCount(0, { timeout: 3000 })
    await expect(page.getByText(/Try changing the budget, time or constraints/)).toHaveCount(0)
  })
})

test.describe('audit: navigation and layout', () => {
  test('[F-06] reading "How it works" and pressing Back keeps the plan and the description', async ({ page }) => {
    knownBug('F-06: Planner state lives in the route component and is discarded on navigation')
    await page.goto('/')
    const mine = "Bangalore, ₹1500, 3 hours this Saturday. I like books and coffee."
    const result = await submitDescription(page, mine)
    expect(result.status).toBe('complete')
    await expect(planHeading(page)).toBeVisible()
    await page.getByRole('link', { name: 'How it works' }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'How it works' })).toBeVisible()
    await page.goBack()
    await expect(description(page)).toHaveValue(mine, { timeout: 3000 })
    await expect(planHeading(page)).toBeVisible()
  })

  test('[F-21] a long unbroken venue name does not make the 390 px page scroll sideways', async ({ page }) => {
    knownBug('F-21: StopCard headings and addresses have no overflow-wrap')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.route('**/api/plan', async (route) => {
      const response = await route.fetch()
      const frames = (await response.text())
        .split('\n\n')
        .filter((f) => f.startsWith('data: '))
        .map((f) => JSON.parse(f.slice(6)) as { type: string; data?: { plan?: { stops: Array<{ candidate: { name: string } }> } } })
      for (const f of frames) {
        if (f.type === 'result' && f.data?.plan) f.data.plan.stops[0].candidate.name = 'Brewhouse'.repeat(9) + '_Koramangala'
      }
      await route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
        body: frames.map((f) => `data: ${JSON.stringify(f)}`).join('\n\n') + '\n\n',
      })
    })
    await page.goto('/')
    await planButton(page).click()
    await expect(planHeading(page)).toBeVisible()
    await expectNoHorizontalOverflow(page)
  })

  test('[F-23] the shared-plan page has a level-1 heading', async ({ page }) => {
    knownBug('F-23: SharedPlan renders PlanView (h2) and ErrorCard (h2) without any h1')
    await page.goto('/')
    const result = await submitDescription(page, BRIEF_TEXT)
    await page.goto(`/p/${String(result.share_id)}`)
    await expect(planHeading(page)).toBeVisible()
    await expect(page.locator('h1')).toHaveCount(1, { timeout: 3000 })
  })
})

test.describe('audit: regression guards (checked and correct)', () => {
  test('a plan that ends after midnight shows "+1" and its calendar file rolls over to Sunday', async ({ page }) => {
    await page.goto('/')
    const result = await submitDescription(page, 'Bangalore, ₹3000, from 9pm for 4 hours. I like live music and late-night food.')
    expect(result.status).toBe('complete')
    await expect(page.getByText(/AM \+1/).first()).toBeVisible()
    const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: 'Add to calendar' }).click()])
    const stream = await download.createReadStream()
    const chunks: Buffer[] = []
    for await (const c of stream) chunks.push(c as Buffer)
    const ics = Buffer.concat(chunks).toString('utf8')
    expect(ics).toContain('TZID:Asia/Kolkata')
    expect(ics).toMatch(/DTEND;TZID=Asia\/Kolkata:20261108T00\d{4}/)
    expect(ics.split('\r\n').every((l) => Buffer.byteLength(l) <= 75)).toBe(true)
  })

  test('markup in every server-returned text field renders literally and unsafe URLs are dropped', async ({ page }) => {
    const dialogs = forbidDialogs(page)
    const X = '<img src=x onerror="window.__xss=1;alert(1)">'
    await page.route('**/api/plan', async (route) => {
      const response = await route.fetch()
      const frames = (await response.text())
        .split('\n\n')
        .filter((f) => f.startsWith('data: '))
        .map((f) => JSON.parse(f.slice(6)) as { type: string; data?: Record<string, unknown> })
      for (const f of frames) {
        if (f.type !== 'result' || !f.data) continue
        const d = f.data as Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any
        const s = d.plan.stops[0]
        Object.assign(s, { why: X, tradeoff: X, warnings: [X] })
        Object.assign(s.candidate, { name: X, address: X, category: X, maps_url: 'javascript:alert(1)' })
        Object.assign(d.plan, { summary_note: X, city: X, maps_route_url: 'javascript:alert(2)', omitted: [{ ref: X, reason: X, name: X }] })
        d.plan.weather.summary = X
        Object.assign(d, { warnings: [X], assumptions: [X], degradations: [X], model: X, checks: [{ name: X, status: 'pass', detail: X }] })
        Object.assign(d.preferences, { mood: X, interests: [X], soft_preferences: [X], neighbourhood: X, city: X })
      }
      await route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
        body: frames.map((f) => `data: ${JSON.stringify(f)}`).join('\n\n') + '\n\n',
      })
    })
    await page.goto('/')
    await planButton(page).click()
    await expect(page.getByRole('heading', { level: 2, name: /Your Saturday in/ })).toBeVisible()
    await expect(page.getByText(X).first()).toBeVisible()
    expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined()
    expect(await page.locator('a[href^="javascript:"]').count()).toBe(0)
    await expect(page.getByRole('link', { name: /Open full route/ })).toHaveCount(0)
    expect(dialogs).toEqual([])
  })
})
