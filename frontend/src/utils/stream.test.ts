import assert from 'node:assert/strict'
import { test } from 'node:test'
import { feedSse } from './stream.ts'

test('完整事件被切出，事件名和 JSON 数据都对', () => {
  const { events, rest } = feedSse('', 'event: accepted\ndata: {"task_id":"t1"}\n\nevent: step\ndata: {"kind":"search"}\n\n')
  assert.deepEqual(events.map((item) => item.event), ['accepted', 'step'])
  assert.deepEqual(events[0].data, { task_id: 't1' })
  assert.equal(rest, '')
})

test('跨块的半个事件留在缓冲区，下一块拼完整', () => {
  const first = feedSse('', 'event: step\ndata: {"ki')
  assert.equal(first.events.length, 0)
  const second = feedSse(first.rest, 'nd":"plan"}\n\nevent: done\ndata: {}\n\n')
  assert.deepEqual(second.events.map((item) => item.event), ['step', 'done'])
  assert.deepEqual(second.events[0].data, { kind: 'plan' })
})

test('坏块丢弃并计数，不拖垮后面的事件', () => {
  const { events, dropped } = feedSse('', 'data: not-json\n\nevent: task\ndata: {"state":"WAITING_FOR_USER"}\n\n')
  assert.equal(dropped, 1)
  assert.deepEqual(events.map((item) => item.event), ['task'])
})
