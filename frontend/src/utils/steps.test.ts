import assert from 'node:assert/strict'
import { test } from 'node:test'
import { stepLines, stepTone, stepViews } from './steps.ts'
import type { TaskStep } from '../api/types'

const search: TaskStep = {
  sequence: 3,
  at: '2026-08-05T01:30:00+00:00',
  kind: 'search',
  title: '搜索交通：Beijing → Shanghai',
  status: 'success',
  detail: {
    date_evidence: '8月5日上午10点前到',
    assumption: '搜索窗口往前 18 小时',
    result_count: 3,
    samples: [{ ref_id: 'MU-EARLY', label: 'Beijing → Shanghai · 08-05 06:20 出发 08:35 到', price: '950', currency: 'USD' }],
  },
}

test('搜索步骤把出处、假设、结果数和样例说出来', () => {
  const lines = stepLines(search)
  assert.ok(lines[0].includes('8月5日上午10点前到'))
  assert.ok(lines.some((line) => line.includes('假设')))
  assert.ok(lines.some((line) => line.includes('结果 3 条')))
  assert.ok(lines.some((line) => line.includes('MU-EARLY') || line.includes('06:20')))
})

test('失败的工具调用标成警示色', () => {
  const failed: TaskStep = {
    sequence: 1,
    at: null,
    kind: 'tool_call',
    title: '模型决定下一步',
    status: 'FAILED',
    detail: { tool_kind: 'LLM', error_code: 'LLM_RATE_LIMITED' },
  }
  assert.equal(stepTone(failed), 'warn')
  assert.ok(stepLines(failed)[0].includes('LLM_RATE_LIMITED'))
})

test('方案步骤逐条列出路线、价格和政策档', () => {
  const plan: TaskStep = {
    sequence: 5,
    at: null,
    kind: 'plan',
    title: '方案生成：1 条',
    status: null,
    detail: {
      options: [{ route: 'Beijing → Shanghai', total_cost: '950', currency: 'USD', policy_outcome: 'COMPLIANT' }],
      open_questions: ['回程哪天走？'],
    },
  }
  const lines = stepLines(plan)
  assert.ok(lines[0].includes('Beijing → Shanghai') && lines[0].includes('合规'))
  assert.ok(lines[1].includes('还没定'))
})

test('视图保持后端顺序并带时间', () => {
  const views = stepViews([search])
  assert.equal(views.length, 1)
  assert.equal(views[0].tone, 'ok')
  assert.notEqual(views[0].time, '')
})
