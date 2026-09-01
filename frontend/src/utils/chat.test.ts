import assert from 'node:assert/strict'
import { test } from 'node:test'
import { chatTurns } from './chat.ts'

test('没有任务时没有对话', () => {
  assert.deepEqual(chatTurns(null), [])
})

test('用户消息按原顺序显示', () => {
  const turns = chatTurns({
    messages: [
      { role: 'user', content: '9月15号去上海', created_at: 'a' },
      { role: 'user', content: '从北京走', created_at: 'b' },
    ],
  })
  assert.deepEqual(turns.map((item) => item.content), ['9月15号去上海', '从北京走'])
  assert.equal(turns.every((item) => item.role === 'user'), true)
})

test('推荐理由排在"还没定的事"之前', () => {
  const turns = chatTurns({
    messages: [
      { role: 'user', content: '去上海', created_at: 'a' },
      { role: 'assistant', content: '回程哪天走？', created_at: 'b' },
    ],
    agentic_proposal: { summary: '推荐前一晚那班', open_questions: ['回程哪天走？'] },
  })
  assert.deepEqual(turns.map((item) => item.content), ['去上海', '推荐前一晚那班', '回程哪天走？'])
  assert.equal(turns[1].kind, 'say')
  assert.equal(turns[2].kind, 'question')
})

test('没有提问时推荐理由放在最后', () => {
  const turns = chatTurns({
    messages: [{ role: 'user', content: '去上海', created_at: 'a' }],
    agentic_proposal: { summary: '三个方案都在右边', open_questions: [] },
  })
  assert.deepEqual(turns.map((item) => item.content), ['去上海', '三个方案都在右边'])
})

test('推荐理由已经在对话里就不重复插', () => {
  const turns = chatTurns({
    messages: [
      { role: 'user', content: '去上海', created_at: 'a' },
      { role: 'assistant', content: '推荐前一晚那班', created_at: 'b' },
    ],
    agentic_proposal: { summary: '推荐前一晚那班', open_questions: [] },
  })
  assert.equal(turns.length, 2)
})

test('空白差异不算两段话', () => {
  const turns = chatTurns({
    messages: [{ role: 'assistant', content: '推荐 前一晚\n那班', created_at: 'b' }],
    agentic_proposal: { summary: '推荐前一晚那班', open_questions: [] },
  })
  assert.equal(turns.length, 1)
})
