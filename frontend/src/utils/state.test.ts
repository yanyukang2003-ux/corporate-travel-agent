import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  formatTravelDate,
  formatTravelTime,
  parseIsoWallClock,
} from './state.ts'

describe('airport-local travel times', () => {
  it('keeps Chicago depart wall clock instead of converting to the browser timezone', () => {
    const iso = '2026-08-24T09:20:00-05:00'
    assert.deepEqual(parseIsoWallClock(iso), {
      year: 2026,
      month: 8,
      day: 24,
      hour: 9,
      minute: 20,
      offset: '-05:00',
    })
    assert.equal(formatTravelTime(iso), '09:20')
    assert.equal(formatTravelDate(iso), '8月24日')
    assert.notEqual(formatTravelTime(iso), '22:20')
  })

  it('keeps Los Angeles arrive wall clock even when that instant is the next morning in China', () => {
    const iso = '2026-08-24T17:30:00-07:00'
    assert.equal(formatTravelTime(iso), '17:30')
    assert.equal(formatTravelDate(iso), '8月24日')
    assert.notEqual(formatTravelTime(iso), '08:30')
  })

  it('treats hotel dates as calendar dates without timezone shifting', () => {
    assert.equal(formatTravelDate('2026-08-24'), '8月24日')
    assert.equal(formatTravelDate('2026-08-25'), '8月25日')
  })
})
