import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { costNotes, durationText, money } from './cost.ts'
import type { CostGuidance } from '../api/types.ts'

function guidance(overrides: Partial<CostGuidance> = {}): CostGuidance {
  return {
    option_id: 'opt-1',
    currency: 'USD',
    premium_over_cheapest_compliant: null,
    cheapest_compliant_option_id: null,
    policy_overages: [],
    approver_id: null,
    tradeoffs: [],
    ...overrides,
  }
}

const labelFor = (id: string) => ({ 'opt-2': '备选方案', 'opt-3': '方案 3' })[id] ?? id

describe('money and duration text', () => {
  it('formats amounts with the currency symbol', () => {
    assert.equal(money('1200', 'USD'), '$1,200')
    assert.equal(money('120.5', 'CNY'), '¥120.5')
  })

  it('does not print NaN when the amount is not a number', () => {
    assert.equal(money('n/a', 'USD'), '$n/a')
  })

  it('reads durations the way people say them', () => {
    assert.equal(durationText(120), '2 小时')
    assert.equal(durationText(100), '1 小时 40 分')
    assert.equal(durationText(40), '40 分钟')
    assert.equal(durationText(0), '')
    // 方向由调用方决定，这里只管长度。
    assert.equal(durationText(-120), '2 小时')
  })
})

describe('what it costs to pick this option', () => {
  it('puts the overspend and the approver in one sentence', () => {
    const notes = costNotes(
      guidance({
        approver_id: 'M2001',
        policy_overages: [
          {
            rule_id: 'hotel.city.nightly_cap',
            amount: '120',
            unit: 'per_night',
            currency: 'USD',
            exception_allowed: true,
          },
        ],
      }),
      labelFor,
    )

    assert.deepEqual(notes, [
      { tone: 'warn', text: '住宿超出差标 $120/晚，需 M2001 审批' },
    ])
  })

  it('falls back to the premium when the breached rule has no number', () => {
    // 舱位超标没有数值阈值——不能因此就不说代价。
    const notes = costNotes(
      guidance({ approver_id: 'M2001', premium_over_cheapest_compliant: '797' }),
      labelFor,
    )

    assert.deepEqual(notes, [
      { tone: 'warn', text: '比最便宜的合规方案贵 $797，需 M2001 审批' },
    ])
  })

  it('says nothing about approval when the option is compliant', () => {
    assert.deepEqual(costNotes(guidance({ premium_over_cheapest_compliant: '0' }), labelFor), [])
  })

  it('never states a saving without its price', () => {
    const notes = costNotes(
      guidance({
        tradeoffs: [
          {
            option_id: 'opt-2',
            saves: '800',
            departure_delta_minutes: 120,
            duration_delta_minutes: 0,
            currency: 'USD',
          },
        ],
      }),
      labelFor,
    )

    assert.deepEqual(notes, [
      { tone: 'hint', text: '换「备选方案」晚走 2 小时，省 $800' },
    ])
  })

  it('spells out that nothing changes when only the price differs', () => {
    const notes = costNotes(
      guidance({
        tradeoffs: [
          {
            option_id: 'opt-2',
            saves: '200',
            departure_delta_minutes: 0,
            duration_delta_minutes: 0,
            currency: 'USD',
          },
        ],
      }),
      labelFor,
    )

    assert.deepEqual(notes, [
      { tone: 'hint', text: '换「备选方案」出发时间不变，省 $200' },
    ])
  })

  it('keeps the direction of an earlier departure', () => {
    const notes = costNotes(
      guidance({
        tradeoffs: [
          {
            option_id: 'opt-3',
            saves: '400',
            departure_delta_minutes: -220,
            duration_delta_minutes: -30,
            currency: 'USD',
          },
        ],
      }),
      labelFor,
    )

    assert.deepEqual(notes, [
      { tone: 'hint', text: '换「方案 3」早走 3 小时 40 分、路上少 30 分钟，省 $400' },
    ])
  })

  it('lists the cost first and the savings after', () => {
    const notes = costNotes(
      guidance({
        approver_id: 'M2001',
        policy_overages: [
          {
            rule_id: 'hotel.city.nightly_cap',
            amount: '120',
            unit: 'per_night',
            currency: 'USD',
            exception_allowed: true,
          },
        ],
        tradeoffs: [
          {
            option_id: 'opt-2',
            saves: '200',
            departure_delta_minutes: 0,
            duration_delta_minutes: 0,
            currency: 'USD',
          },
        ],
      }),
      labelFor,
    )

    assert.deepEqual(
      notes.map((item) => item.tone),
      ['warn', 'hint'],
    )
  })

  it('says nothing at all when the backend could not compute anything', () => {
    // 币种不一致时后端不给数，这里就一句都不写，不去凑。
    assert.deepEqual(costNotes(guidance(), labelFor), [])
  })
})
