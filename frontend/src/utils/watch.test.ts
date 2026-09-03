import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  canCancel,
  canReportChange,
  eventLine,
  legLine,
  localInputToIso,
  observationFor,
  tripStatusLabel,
  verdictLabel,
  verdictTone,
} from './watch.ts'
import type { FlightObservation, TripAggregate, TripEvent, WatchLeg } from '../api/types.ts'

const leg: WatchLeg = {
  ref_id: 'MU-EARLY',
  provider: 'mock',
  origin: 'Beijing',
  destination: 'Shanghai',
  depart_at: '2026-08-05T06:20:00+08:00',
  arrive_at: '2026-08-05T08:35:00+08:00',
}

const observation: FlightObservation = {
  ref_id: 'MU-EARLY',
  status: 'DELAYED',
  observed_at: '2026-08-03T06:20:00+08:00',
  source: 'memory',
  verdict: 'NOTIFY_ONLY',
  reasons: ['MU-EARLY 延误 30 分钟'],
  estimated_depart_at: null,
  estimated_arrive_at: '2026-08-05T09:05:00+08:00',
  opened_task_id: null,
}

function trip(overrides: Partial<TripAggregate> = {}): TripAggregate {
  return {
    trip_id: 'trip-1',
    traveler_id: 'E1001',
    requester_id: 'E1001',
    status: 'BOOKED',
    task_ids: ['task-1'],
    created_at: '2026-08-01T09:00:00+08:00',
    watch: {
      task_id: 'task-1',
      legs: [leg],
      registered_at: '2026-08-01T09:00:00+08:00',
      watch_until: '2026-08-07T20:20:00+08:00',
      next_check_at: '2026-08-03T06:20:00+08:00',
      last_checked_at: null,
      check_count: 0,
      observations: [observation],
    },
    events: [],
    ...overrides,
  }
}

describe('legLine', () => {
  it('names the ticket, route, times and the latest status', () => {
    assert.equal(
      legLine(leg, observation),
      'MU-EARLY Beijing→Shanghai 08-05 06:20 → 08-05 08:35 · 延误 / 改时，预计 08-05 09:05 到',
    )
  })
  it('says so when nothing has been checked yet', () => {
    assert.match(legLine(leg, null), /还没查过动态$/)
  })
})

describe('observationFor', () => {
  it('finds the observation by ticket and tolerates a missing watch', () => {
    assert.equal(observationFor(trip(), 'MU-EARLY')?.verdict, 'NOTIFY_ONLY')
    assert.equal(observationFor(trip({ watch: null }), 'MU-EARLY'), null)
    assert.equal(observationFor(null, 'MU-EARLY'), null)
  })
})

describe('eventLine', () => {
  const base: TripEvent = {
    event_id: 'e1',
    event_type: 'FLIGHT_CHANGED',
    received_at: '2026-08-03T06:20:00+08:00',
    reported_by: 'flight-status:memory',
    ref_id: 'MU-EARLY',
    new_depart_at: null,
    new_arrive_by: null,
    note: '已取消',
    opened_task_id: 'abcdef123456',
    leg_index: null,
    impact: null,
  }
  it('describes a carrier-fed flight change with the change task it opened', () => {
    assert.equal(eventLine(base), '航班变更 MU-EARLY · 航班动态源 报，已开改期任务 abcdef12 · 已取消')
  })
  it('names the leg of a meeting move and the person who reported it', () => {
    assert.equal(
      eventLine({ ...base, event_type: 'MEETING_MOVED', ref_id: null, reported_by: 'E1001', leg_index: 1, note: null, opened_task_id: null }),
      '会议改期（第 2 段） · E1001 报',
    )
  })
  it('describes a cancellation', () => {
    assert.equal(
      eventLine({ ...base, event_type: 'TRIP_CANCELLED', ref_id: null, reported_by: 'E1001', note: '项目取消', opened_task_id: null }),
      '差旅取消 · E1001 报 · 项目取消',
    )
  })
})

describe('labels and tones', () => {
  it('map trip status and verdicts to plain words', () => {
    assert.equal(tripStatusLabel('BOOKED'), '已订 · 观察中')
    assert.equal(tripStatusLabel('CANCELLED'), '已取消')
    assert.equal(verdictLabel('REBOOK_REQUIRED'), '需要改期')
    assert.equal(verdictTone('NOTIFY_ONLY'), 'orange')
    assert.equal(verdictTone('NO_CHANGE'), 'gray')
  })
})

describe('localInputToIso', () => {
  it('appends seconds and the timezone offset', () => {
    assert.equal(localInputToIso('2026-08-07T10:00'), '2026-08-07T10:00:00+08:00')
    assert.equal(localInputToIso('2026-08-07T10:00:30', '+09:00'), '2026-08-07T10:00:30+09:00')
    assert.equal(localInputToIso('not a time'), null)
  })
})

describe('guards', () => {
  it('only booked trips accept changes; anything but cancelled can be cancelled', () => {
    assert.equal(canReportChange(trip()), true)
    assert.equal(canReportChange(trip({ status: 'CHANGE_REQUESTED' })), false)
    assert.equal(canReportChange(null), false)
    assert.equal(canCancel(trip({ status: 'PLANNED' })), true)
    assert.equal(canCancel(trip({ status: 'CANCELLED' })), false)
  })
})
