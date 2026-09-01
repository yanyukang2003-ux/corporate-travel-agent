import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { approvalReasonText, factsForOptionCard, openQuestionsFromTask } from './notices.ts'

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

describe('what the approver reads first', () => {
  it('says the system could not judge, in Chinese, not the English log line', () => {
    const text = approvalReasonText({
      policy_outcome: 'INSUFFICIENT_EVIDENCE',
      facts: [
        'total_cost=2350',
        'unjudged_rules=hotel.city.nightly_cap',
        '公司政策里还没有 Chengdu 的酒店夜费上限，这几晚是否超标我判不了。',
      ],
      rule_evidence: [
        { outcome: 'INSUFFICIENT_EVIDENCE', message: 'No hotel cap is configured for Chengdu.' },
      ],
    })
    assert.equal(text, '公司政策里还没有 Chengdu 的酒店夜费上限，这几晚是否超标我判不了。')
  })

  it('still shows the violated rule when the option is a real policy exception', () => {
    const text = approvalReasonText({
      policy_outcome: 'REQUIRES_APPROVAL',
      facts: ['total_cost=2750'],
      rule_evidence: [
        { outcome: 'REQUIRES_APPROVAL', message: 'HT-NEAR: nightly price 900 exceeds cap 800.' },
      ],
    })
    assert.equal(text, 'HT-NEAR: nightly price 900 exceeds cap 800.')
  })
})
