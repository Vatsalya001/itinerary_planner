import { expect, test } from '@playwright/test'
import { expectNoHorizontalOverflow, planHeading, submitDescription } from './helpers'

const KEY = 'saturday-planner:itinerary-history:v1'

test('city history survives reload, opens without replanning, and can be removed', async ({ page }) => {
  let plans = 0
  page.on('request', request => {
    if (request.method() === 'POST' && /\/api\/plan$/.test(new URL(request.url()).pathname)) plans += 1
  })
  await page.goto('/')
  const result = await submitDescription(page, 'Plan Saturday in Mumbai from 3 PM to 7 PM. Budget INR 3000. I enjoy vegetarian food and museums.')
  expect(result.status).toBe('complete')
  const history = page.getByRole('region', { name: 'Your itinerary history' })
  const open = history.getByRole('link', { name: /Open itinerary for Mumbai/ })
  await expect(open).toHaveAttribute('href', `/p/${result.share_id}`)
  const saved = await page.evaluate(key => JSON.parse(localStorage.getItem(key)!), KEY)
  expect(saved.entries).toHaveLength(1)
  expect(Object.keys(saved.entries[0]).sort()).toEqual(['city', 'date', 'prompt', 'savedAt', 'shareId'])
  await page.reload()
  await expect(open).toBeVisible()
  await open.click()
  await expect(planHeading(page)).toContainText('Mumbai')
  expect(plans).toBe(1)
  await page.goto('/')
  await page.setViewportSize({ width: 390, height: 844 })
  await expectNoHorizontalOverflow(page)
  await history.scrollIntoViewIfNeeded()
  await page.screenshot({ path: '/tmp/saturday-history-mobile.png', fullPage: true })
  await history.getByRole('button', { name: /Remove itinerary for Mumbai/ }).click()
  await page.reload()
  await expect(history.getByRole('link')).toHaveCount(0)
  expect(plans).toBe(1)
})

test('an expired history link has a clear error and clear-history requires confirmation', async ({ page }) => {
  await page.goto('/')
  await page.evaluate(key => localStorage.setItem(key, JSON.stringify({ version: 1, entries: [{
    shareId: 'missing_history_plan', city: 'Gurugram', date: '2026-11-07', prompt: 'Food and art', savedAt: Date.now(),
  }] })), KEY)
  await page.reload()
  const history = page.getByRole('region', { name: 'Your itinerary history' })
  await history.getByRole('link', { name: /Open itinerary for Gurugram/ }).click()
  await expect(page.getByRole('heading', { name: "This plan isn't available" })).toBeVisible()
  await page.goto('/')
  await history.getByRole('button', { name: 'Clear history', exact: true }).click()
  await history.getByRole('button', { name: 'Keep history', exact: true }).click()
  await expect(history.getByRole('link')).toHaveCount(1)
  await history.getByRole('button', { name: 'Clear history', exact: true }).click()
  await history.getByRole('button', { name: 'Yes, clear history', exact: true }).click()
  await page.reload()
  await expect(history.getByRole('link')).toHaveCount(0)
})
