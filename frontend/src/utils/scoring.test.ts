import assert from 'node:assert/strict'
import { test } from 'node:test'
import { categoriesFromFacts, rankedBreakdowns, scoreBreakdown } from './scoring.ts'

const option = {
  option_id: 'opt-1',
  score: '663.05',
  total_cost: '632.95',
  total_duration_minutes: 301,
  preference_penalty: '0',
  facts: ['total_cost=632.95', 'category=best_overall|fastest'],
}

test('按后端公式重算，能和 score 对上', () => {
  const row = scoreBreakdown(option, 10)
  assert.equal(row.cost, 632.95)
  assert.ok(Math.abs(row.durationScore - 30.1) < 1e-9)
  assert.equal(row.total, 663.05)
  assert.equal(row.reconciles, true)
})

test('对不上时如实说对不上', () => {
  const row = scoreBreakdown({ ...option, score: '999' }, 10)
  assert.equal(row.reconciles, false)
})

test('换算比例变了，时长那一项跟着变', () => {
  assert.ok(Math.abs(scoreBreakdown(option, 60).durationScore - 301 / 60) < 1e-9)
  assert.ok(Math.abs(scoreBreakdown(option, 1).durationScore - 301) < 1e-9)
})

test('比例为零时不做除法', () => {
  assert.equal(scoreBreakdown(option, 0).durationScore, 0)
})

test('类别翻成人话', () => {
  assert.deepEqual(categoriesFromFacts(option.facts), ['综合最合适', '最快'])
  assert.deepEqual(categoriesFromFacts(['total_cost=1']), [])
  assert.deepEqual(categoriesFromFacts(undefined), [])
})

test('按总分升序：分低者胜', () => {
  const rows = rankedBreakdowns(
    [
      { ...option, option_id: 'b', score: '695.85', total_cost: '617.25', total_duration_minutes: 786 },
      { ...option, option_id: 'a' },
    ],
    10,
  )
  assert.deepEqual(rows.map((row) => row.optionId), ['a', 'b'])
})
