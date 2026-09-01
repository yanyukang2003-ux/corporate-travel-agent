import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { confirmationSummary, parseOrderReferences } from './booking.ts'
import type { BookingConfirmation } from '../api/types.ts'

function confirmation(overrides: Partial<BookingConfirmation> = {}): BookingConfirmation {
  return {
    confirmation_id: 'conf-1',
    intent_id: 'intent-1',
    option_id: 'opt-1',
    option_version: 1,
    order_references: ['PNR-1'],
    total_amount: '1180',
    currency: 'USD',
    source: 'SELF_REPORTED',
    reported_by: 'E1001',
    reported_at: '2026-08-01T09:00:00+08:00',
    booked_at: '2026-08-01T09:00:00+08:00',
    note: null,
    planned_total: '1200',
    planned_currency: 'USD',
    cost_variance: '-20',
    ...overrides,
  }
}

describe('parseOrderReferences', () => {
  it('splits on commas, semicolons and newlines, trims, and drops empties', () => {
    assert.deepEqual(parseOrderReferences(' PNR-1 ,HTL-9\n\n；X；'), ['PNR-1', 'HTL-9', 'X'])
  })

  it('keeps the first occurrence of a duplicate and preserves order', () => {
    assert.deepEqual(parseOrderReferences('B, A, B, A'), ['B', 'A'])
  })

  it('returns nothing for blank input', () => {
    assert.deepEqual(parseOrderReferences('  ,  \n '), [])
  })
})

describe('confirmationSummary', () => {
  it('says how much less was paid than planned', () => {
    assert.equal(confirmationSummary(confirmation()), '实付 $1,180，比方案价少付 $20')
  })

  it('says how much more was paid than planned', () => {
    assert.equal(
      confirmationSummary(confirmation({ total_amount: '1250', cost_variance: '50' })),
      '实付 $1,250，比方案价多付 $50',
    )
  })

  it('says the amounts match when the variance is zero', () => {
    assert.equal(
      confirmationSummary(confirmation({ total_amount: '1200', cost_variance: '0' })),
      '实付 $1,200，和方案价一致',
    )
  })

  it('refuses to compare across currencies instead of converting', () => {
    assert.equal(
      confirmationSummary(confirmation({ total_amount: '8600', currency: 'CNY', cost_variance: null })),
      '实付 ¥8,600，方案价 $1,200；币种不同，不比较',
    )
  })

  it('reports only the paid amount when the plan is missing', () => {
    assert.equal(
      confirmationSummary(confirmation({ planned_total: null, planned_currency: null, cost_variance: null })),
      '实付 $1,180',
    )
  })
})

import { reconciliationText } from './booking.ts'
import type { ExpenseReconciliation } from '../api/types.ts'

function reconciliation(overrides: Partial<ExpenseReconciliation> = {}): ExpenseReconciliation {
  return {
    reconciliation_id: 'r-1',
    expense_id: 'EXP-1',
    source_system: 'expense-system',
    expense_amount: '1200',
    currency: 'USD',
    expensed_at: '2026-08-10T00:00:00+00:00',
    reconciled_at: '2026-08-11T00:00:00+00:00',
    status: 'MATCHED',
    matched_order_references: ['PNR-1'],
    note: null,
    amount_variance: '0',
    ...overrides,
  }
}

describe('reconciliationText', () => {
  it('says the expense matches the self-reported amount', () => {
    assert.equal(reconciliationText(reconciliation(), 'USD'), '费控 $1,200，和自述一致')
  })
  it('says how much more or less the expense system recorded', () => {
    assert.equal(
      reconciliationText(reconciliation({ expense_amount: '1235', status: 'AMOUNT_MISMATCH', amount_variance: '35' }), 'USD'),
      '费控 $1,235，比自述多 $35',
    )
  })
  it('refuses to compare across currencies', () => {
    assert.equal(
      reconciliationText(reconciliation({ currency: 'CNY', status: 'CURRENCY_MISMATCH', amount_variance: null }), 'USD'),
      '费控 ¥1,200，币种和自述不同，不比较',
    )
  })
})
