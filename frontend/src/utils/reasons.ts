/**
 * 每条方案的理由和优点：从后端数据**推导**，不编内容。
 *
 * 后端的 `facts` 是机器键值串（total_cost=238.80 这种），直接上屏没人看得懂。
 * 但数据里有能说成人话的东西：`category=` 说了它是最便宜还是最快，`cost_guidance`
 * 说了比最便宜贵多少，航段上有直达与否和出发时刻。这里只翻译和比较，凡是数据里
 * 没有的一个字不加。
 */

import { categoriesFromFacts } from './scoring.ts'

/** 推导理由所需的最小方案形状（`TravelOption` 的子集）。 */
export interface OptionForReasons {
  option_id: string
  facts: string[]
  total_cost: string | number
  total_duration_minutes: number
  currency: string
  legs: { is_direct: boolean; depart_at: string }[]
  cost_guidance: { premium_over_cheapest_compliant?: string | null }
  policy_outcome: string
}

const SYMBOLS: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€', GBP: '£' }

function symbol(currency: string): string {
  return SYMBOLS[currency] ?? `${currency} `
}

function departDate(option: OptionForReasons): string {
  return (option.legs[0]?.depart_at ?? '').slice(0, 10)
}

function departClock(option: OptionForReasons): string {
  return (option.legs[0]?.depart_at ?? '').slice(11, 16)
}

function cheapestOf(all: OptionForReasons[]): OptionForReasons | null {
  let best: OptionForReasons | null = null
  for (const option of all) {
    if (best === null || Number(option.total_cost) < Number(best.total_cost)) best = option
  }
  return best
}

/**
 * 一条方案的理由列表（最多 4 条）。
 *
 * 规则全部可核验：
 * 1. 后端类别（综合最合适 / 最便宜 / 最快）原样翻译；「备选」不算理由，不展示。
 * 2. 比最便宜贵多少来自 `cost_guidance.premium_over_cheapest_compliant`；
 *    贵得有回报时（更快 / 不用赶早班）把回报一起说。
 * 3. 全程直达从航段的 `is_direct` 数出来。
 * 4. 各方案出发日期不同时，说清这条是当天出发还是要前一晚走。
 */
export function optionReasons(option: OptionForReasons, all: OptionForReasons[]): string[] {
  const reasons: string[] = []
  const categories = categoriesFromFacts(option.facts).filter((label) => label !== '备选')
  reasons.push(...categories)

  const cheapest = cheapestOf(all)
  const premium = Number(option.cost_guidance?.premium_over_cheapest_compliant ?? Number.NaN)
  if (cheapest && cheapest.option_id !== option.option_id && Number.isFinite(premium) && premium > 0) {
    let line = `比最便宜方案贵 ${symbol(option.currency)}${premium.toFixed(2)}`
    const faster = cheapest.total_duration_minutes - option.total_duration_minutes
    const laterStart = departClock(option) > departClock(cheapest)
      && departDate(option) === departDate(cheapest)
      && departClock(cheapest) < '09:00'
    if (faster > 0) {
      line += `，全程快 ${faster} 分钟`
    } else if (laterStart) {
      line += `，${departClock(option)} 出发不用赶早班（最便宜那班 ${departClock(cheapest)}）`
    }
    reasons.push(line)
  }

  if (option.legs.length > 0 && option.legs.every((leg) => leg.is_direct)) {
    reasons.push(option.legs.length > 1 ? '每一段都直达' : '直达')
  }

  const dates = new Set(all.map(departDate).filter(Boolean))
  if (dates.size > 1) {
    const latest = [...dates].sort().at(-1)
    reasons.push(departDate(option) === latest ? '当天出发' : '需前一晚出发')
  }

  return reasons.slice(0, 4)
}
