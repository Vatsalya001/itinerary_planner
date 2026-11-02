// @vitest-environment node
import { describe, expect, it } from 'vitest'
import {
  categoryLabel,
  costBasisLabel,
  fmtClock,
  fmtDate,
  fmtDuration,
  fmtMoney,
  fmtMoneyRange,
  fmtPercent,
  groupDigits,
  sourceLabel,
  weatherLine,
} from './format'

describe('fmtClock', () => {
  it('formats minutes since midnight as 12-hour clock time', () => {
    expect(fmtClock(900)).toBe('3:00 PM')
    expect(fmtClock(0)).toBe('12:00 AM')
    expect(fmtClock(720)).toBe('12:00 PM')
    expect(fmtClock(605)).toBe('10:05 AM')
    expect(fmtClock(1439)).toBe('11:59 PM')
  })

  it('adds a +1 suffix after midnight', () => {
    expect(fmtClock(1440)).toBe('12:00 AM +1')
    expect(fmtClock(1530)).toBe('1:30 AM +1')
    expect(fmtClock(2880 + 60)).toBe('1:00 AM +2')
  })
})

describe('fmtDuration', () => {
  it('formats minutes', () => {
    expect(fmtDuration(45)).toBe('45 min')
    expect(fmtDuration(60)).toBe('1 h')
    expect(fmtDuration(90)).toBe('1 h 30 min')
    expect(fmtDuration(240)).toBe('4 h')
  })
})

describe('fmtMoney', () => {
  it('uses Indian digit grouping for INR', () => {
    expect(fmtMoney(2000, 'INR')).toBe('₹2,000')
    expect(fmtMoney(100000, 'INR')).toBe('₹1,00,000')
    expect(fmtMoney(12345678, 'INR')).toBe('₹1,23,45,678')
    expect(fmtMoney(999, 'INR')).toBe('₹999')
    expect(fmtMoney(0, 'INR')).toBe('₹0')
  })

  it('uses western grouping and symbols for other currencies', () => {
    expect(fmtMoney(1234567, 'USD')).toBe('$1,234,567')
    expect(fmtMoney(100000, 'GBP')).toBe('£100,000')
    expect(fmtMoney(45, 'EUR')).toBe('€45')
    expect(fmtMoney(200, 'AED')).toBe('AED 200')
  })

  it('handles negatives and rounding', () => {
    expect(fmtMoney(-1500, 'INR')).toBe('-₹1,500')
    expect(fmtMoney(99.6, 'USD')).toBe('$100')
  })

  it('groups digits', () => {
    expect(groupDigits('1234567', true)).toBe('12,34,567')
    expect(groupDigits('1234567', false)).toBe('1,234,567')
  })

  it('formats ranges', () => {
    expect(fmtMoneyRange(0, 0, 'INR')).toBe('Free')
    expect(fmtMoneyRange(499, 499, 'INR')).toBe('₹499')
    expect(fmtMoneyRange(250, 1450, 'INR')).toBe('₹250–₹1,450')
  })
})

describe('labels', () => {
  it('maps cost basis and source providers to the UI wording', () => {
    expect(costBasisLabel('listed_price_range')).toBe('listed price')
    expect(costBasisLabel('price_level_estimate')).toBe('price level estimate')
    expect(costBasisLabel('ticket_price')).toBe('ticket price')
    expect(costBasisLabel('free')).toBe('free')
    expect(costBasisLabel('category_estimate')).toBe('estimate')
    expect(sourceLabel('google_places')).toBe('Live · Google Maps')
    expect(sourceLabel('curated_snapshot')).toBe('Curated · OpenStreetMap')
    expect(sourceLabel('web_event')).toBe('Web event · check before you go')
    expect(sourceLabel('fixture')).toBe('Test data')
    expect(categoryLabel('cafe')).toBe('Café')
    expect(categoryLabel('live_music_venue')).toBe('Live music')
    expect(categoryLabel('museum')).toBe('Museum')
  })

  it('formats dates without timezone drift', () => {
    expect(fmtDate('2026-11-07')).toBe('Saturday, 7 November 2026')
  })

  it('formats percentages and weather', () => {
    expect(fmtPercent(0.953)).toBe('95%')
    expect(fmtPercent(null)).toBe('—')
    expect(weatherLine({ status: 'unavailable', summary: '' })).toBe('Weather not checked')
    expect(
      weatherLine({ status: 'ok', summary: 'Partly cloudy', temp_min_c: 19, temp_max_c: 28, precip_prob_max: 20, sunset_min: 1065 }),
    ).toBe('Partly cloudy · 19–28°C · 20% chance of rain · sunset 5:45 PM')
  })
})
