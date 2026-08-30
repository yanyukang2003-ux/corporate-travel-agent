import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { factsForOptionCard, openQuestionsFromTask } from './notices.ts'

describe('open questions next to options', () => {
  it('splits the host question into one notice per line', () => {
    assert.deepEqual(
      openQuestionsFromTask({
        clarification_question:
          'Shanghai→Hangzhou 这段没有可用机票。短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，或者告诉我换一个日期再搜。\n杭州打算哪天去？',
      }),
      [
        'Shanghai→Hangzhou 这段没有可用机票。短途通常更适合高铁；系统目前查不了火车票，这一段需要你自己安排，或者告诉我换一个日期再搜。',
        '杭州打算哪天去？',
      ],
    )
  })

  it('is empty when there is nothing left to ask', () => {
    assert.deepEqual(openQuestionsFromTask({ clarification_question: null }), [])
    assert.deepEqual(openQuestionsFromTask({ clarification_question: '  \n  ' }), [])
  })
})

describe('option-card facts', () => {
  it('shows the empty-leg sentence instead of total_cost= when both are present', () => {
    const notice = 'Shanghai→Hangzhou 这段没有可用机票。系统目前查不了火车票。'
    assert.deepEqual(
      factsForOptionCard([
        notice,
        'total_cost=950',
        'currency=CNY',
        'outbound=MU-EARLY',
      ]),
      [notice],
    )
  })

  it('keeps machine facts when the option has no human sentence', () => {
    assert.deepEqual(
      factsForOptionCard(['total_cost=950', 'currency=CNY', 'outbound=MU-EARLY', 'policy=COMPLIANT']),
      ['total_cost=950', 'currency=CNY', 'outbound=MU-EARLY'],
    )
  })
})
