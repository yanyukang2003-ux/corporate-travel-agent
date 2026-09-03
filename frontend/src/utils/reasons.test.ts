import assert from 'node:assert/strict'
import { test } from 'node:test'
import { optionReasons } from './reasons.ts'
import type { OptionForReasons } from './reasons.ts'

/** 按 2026-09-01 真实任务的数据形状造三条方案。 */
function option(partial: Partial<OptionForReasons> & { option_id: string }): OptionForReasons {
  return {
    facts: [],
    total_cost: '134.35',
    total_duration_minutes: 240,
    currency: 'USD',
    legs: [{ is_direct: true, depart_at: '2026-09-09T06:11:00+08:00' }],
    cost_guidance: { premium_over_cheapest_compliant: '0.00' },
    policy_outcome: 'COMPLIANT',
    ...partial,
  }
}

const cheapest = option({
  option_id: 'opt-a',
  facts: ['total_cost=134.35', 'category=best_overall|cheapest'],
})
const noEarly = option({
  option_id: 'opt-b',
  facts: ['category=alternative'],
  total_cost: '304.88',
  total_duration_minutes: 261,
  legs: [{ is_direct: true, depart_at: '2026-09-09T12:50:00+08:00' }],
  cost_guidance: { premium_over_cheapest_compliant: '170.53' },
})
const fastest = option({
  option_id: 'opt-c',
  facts: ['category=fastest'],
  total_cost: '404.07',
  total_duration_minutes: 224,
  cost_guidance: { premium_over_cheapest_compliant: '269.72' },
})
const all = [cheapest, noEarly, fastest]

test('最便宜的方案：类别翻成人话，机器串一条不上屏', () => {
  const reasons = optionReasons(cheapest, all)
  assert.ok(reasons.includes('综合最合适'))
  assert.ok(reasons.includes('最便宜'))
  assert.ok(reasons.every((item) => !item.includes('=')))
})

test('贵一些的备选：说清贵多少、换来什么（不用赶早班）', () => {
  const reasons = optionReasons(noEarly, all)
  const premium = reasons.find((item) => item.includes('贵 $170.53'))
  assert.ok(premium)
  assert.ok(premium.includes('12:50 出发不用赶早班'))
  assert.ok(!reasons.includes('备选'))
})

test('最快的方案：贵多少和快多少一起说', () => {
  const reasons = optionReasons(fastest, all)
  assert.ok(reasons.includes('最快'))
  assert.ok(reasons.some((item) => item.includes('贵 $269.72') && item.includes('快 16 分钟')))
})

test('出发日期不同时说清当天还是前一晚', () => {
  const sameDay = option({ option_id: 'd1', legs: [{ is_direct: true, depart_at: '2026-09-15T07:00:00+08:00' }] })
  const prevEve = option({
    option_id: 'd2',
    total_cost: '238.80',
    legs: [{ is_direct: true, depart_at: '2026-09-14T16:40:00+08:00' }],
    cost_guidance: { premium_over_cheapest_compliant: null },
  })
  assert.ok(optionReasons(sameDay, [sameDay, prevEve]).includes('当天出发'))
  assert.ok(optionReasons(prevEve, [sameDay, prevEve]).includes('需前一晚出发'))
})
