/**
 * 把「选这条要付出什么」写成人话。
 *
 * 后端算出的是数：超出 120、省 200、晚走 120 分钟、审批人 M2001。
 * 这里只做一件事——把这些数拼成员工在**做选择的那一刻**读得懂的一句话。
 *
 * 两条规矩，和后端 `planning/cost_guidance.py` 是同一条：
 *
 * 1. **省钱不能单独出现。** "省 ¥200" 后面一定跟着代价（晚走多久、路上多久），
 *    没有代价就写"出发时间不变"。只说省钱是诱导，不是引导。
 * 2. **算不出来就不说。** 币种不一致时后端不给数，这里也就一句话都不写，
 *    不去凑一个。
 */

import type { CostGuidance, PolicyOverage, Tradeoff } from '../api/types.ts'

/** 一条给人看的代价说明。`warn` 是"要付出的代价"，`hint` 是"可以省"。 */
export interface CostNote {
  tone: 'warn' | 'hint'
  text: string
}

const RULE_NAMES: Record<string, string> = {
  'hotel.city.nightly_cap': '住宿',
  'transport.flight.seat_class': '舱位',
  'transport.train.seat_class': '席别',
}

const UNIT_SUFFIX: Record<string, string> = {
  per_night: '/晚',
  total: '',
}

function symbolFor(currency: string): string {
  return { CNY: '¥', USD: '$', EUR: '€', GBP: '£' }[currency.toUpperCase()] ?? `${currency} `
}

/** 带货币符号的金额；数字不合法时原样返回，不显示 NaN。 */
export function money(amount: string, currency: string): string {
  const value = Number(amount)
  if (!Number.isFinite(value)) return `${symbolFor(currency)}${amount}`
  return `${symbolFor(currency)}${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)}`
}

/** 分钟数写成"2 小时""1 小时 40 分""40 分钟"。只接受正数，方向由调用方决定。 */
export function durationText(minutes: number): string {
  const total = Math.abs(Math.round(minutes))
  if (total === 0) return ''
  const hours = Math.floor(total / 60)
  const rest = total % 60
  if (hours === 0) return `${rest} 分钟`
  if (rest === 0) return `${hours} 小时`
  return `${hours} 小时 ${rest} 分`
}

/** 超标一条写一句："住宿超出差标 $120/晚"。 */
function overageText(overage: PolicyOverage): string {
  const name = RULE_NAMES[overage.rule_id] ?? overage.rule_id
  const suffix = UNIT_SUFFIX[overage.unit] ?? ''
  return `${name}超出差标 ${money(overage.amount, overage.currency)}${suffix}`
}

/**
 * 换一条能省多少，以及代价。
 *
 * 代价按员工真正在意的顺序说：先"几点出门"（出发时刻），再"路上多久"。
 * 两样都没变化时明说"出发时间不变"——留白会让人以为是漏了。
 */
function tradeoffText(tradeoff: Tradeoff, label: string): string {
  const saves = money(tradeoff.saves, tradeoff.currency)
  const costs: string[] = []
  const departure = durationText(tradeoff.departure_delta_minutes)
  if (departure) {
    costs.push(tradeoff.departure_delta_minutes > 0 ? `晚走 ${departure}` : `早走 ${departure}`)
  }
  const duration = durationText(tradeoff.duration_delta_minutes)
  if (duration) {
    costs.push(tradeoff.duration_delta_minutes > 0 ? `路上多 ${duration}` : `路上少 ${duration}`)
  }
  const cost = costs.length > 0 ? costs.join('、') : '出发时间不变'
  return `换「${label}」${cost}，省 ${saves}`
}

/**
 * 一条方案的全部代价说明，按"先代价、后省法"排。
 *
 * `labelFor` 把方案 ID 换成卡片上的名字（"备选方案"），因为 ID 对员工没有意义。
 */
export function costNotes(
  guidance: CostGuidance,
  labelFor: (optionId: string) => string,
): CostNote[] {
  const notes: CostNote[] = []

  const approver = guidance.approver_id ? `，需 ${guidance.approver_id} 审批` : ''
  if (guidance.policy_overages.length > 0) {
    // 有具体数值的超标优先——"超出差标 $120/晚"比"贵了 $200"更接近员工要解释的那件事。
    notes.push({
      tone: 'warn',
      text: `${guidance.policy_overages.map(overageText).join('；')}${approver}`,
    })
  } else if (guidance.approver_id) {
    // 舱位这类超标没有数值阈值，只能用"比最便宜的合规方案贵多少"说明代价。
    const premium = guidance.premium_over_cheapest_compliant
    const extra =
      premium && Number(premium) > 0
        ? `比最便宜的合规方案贵 ${money(premium, guidance.currency)}`
        : '超出差旅标准'
    notes.push({ tone: 'warn', text: `${extra}${approver}` })
  }

  for (const tradeoff of guidance.tradeoffs) {
    notes.push({ tone: 'hint', text: tradeoffText(tradeoff, labelFor(tradeoff.option_id)) })
  }
  return notes
}
