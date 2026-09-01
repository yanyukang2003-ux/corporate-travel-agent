/**
 * 下单确认的两个小工具：把员工填的订单号切成列表，把「实付 vs 方案价」写成人话。
 *
 * 规矩和后端一致（`domain/validation.py`、`TripTask.booking_cost_variance`）：
 * 订单号去空白、去重、去空串；差额只在币种一致时说，不一致就明说"不比较"，不换算。
 */

import type { BookingConfirmation, ExpenseReconciliation } from '../api/types.ts'
import { money } from './cost.ts'

/** 把一行文字切成订单号列表：逗号、分号、换行都算分隔；去空白、去重、去空串，顺序保留。 */
export function parseOrderReferences(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of text.split(/[\n,，;；]+/)) {
    const value = raw.trim()
    if (!value || seen.has(value)) continue
    seen.add(value)
    out.push(value)
  }
  return out
}

/**
 * 一句话说清实付和方案价的关系。
 *
 * 四种情况，每种都要说出口：没有方案价（只报实付）、币种不同（明说不比较）、
 * 一致、多付或少付了多少。
 */
export function confirmationSummary(confirmation: BookingConfirmation): string {
  const paid = money(String(confirmation.total_amount), confirmation.currency)
  if (confirmation.planned_total == null) return `实付 ${paid}`
  const plannedCurrency = confirmation.planned_currency ?? confirmation.currency
  const planned = money(String(confirmation.planned_total), plannedCurrency)
  if (confirmation.cost_variance == null) {
    return `实付 ${paid}，方案价 ${planned}；币种不同，不比较`
  }
  const variance = Number(confirmation.cost_variance)
  if (!Number.isFinite(variance) || variance === 0) return `实付 ${paid}，和方案价一致`
  const delta = money(String(Math.abs(variance)), confirmation.currency)
  return variance > 0
    ? `实付 ${paid}，比方案价多付 ${delta}`
    : `实付 ${paid}，比方案价少付 ${delta}`
}

/** 费控对账结果写成一句：对上了 / 差多少 / 币种不同不比较。 */
export function reconciliationText(reconciliation: ExpenseReconciliation, currency: string): string {
  const expensed = money(String(reconciliation.expense_amount), reconciliation.currency)
  if (reconciliation.status === 'CURRENCY_MISMATCH' || reconciliation.amount_variance == null) {
    return `费控 ${expensed}，币种和自述不同，不比较`
  }
  const variance = Number(reconciliation.amount_variance)
  if (!Number.isFinite(variance) || variance === 0) return `费控 ${expensed}，和自述一致`
  const delta = money(String(Math.abs(variance)), currency)
  return variance > 0 ? `费控 ${expensed}，比自述多 ${delta}` : `费控 ${expensed}，比自述少 ${delta}`
}
