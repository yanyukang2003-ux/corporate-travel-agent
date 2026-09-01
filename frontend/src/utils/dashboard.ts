/** 管理看板的纯函数：指标怎么显示、预算用了几成、位置状态叫什么。 */

import type { MetricResult, WhereaboutsStatus } from '../api/types'

/** 看板首屏放哪几个指标、按什么顺序、叫什么。其余指标在下方全表里用后端的长标签。 */
export const KPI_CARDS: readonly { key: string; title: string }[] = [
  { key: 'booking_confirmation_rate', title: '回填了订单号的比例' },
  { key: 'approval_request_rate', title: '走了例外审批的比例' },
  { key: 'off_channel_expense_rate', title: '渠道外预订率' },
  { key: 'change_intervention_rate', title: '变更场景人工介入率' },
  { key: 'advance_booking_days_mean', title: '提前几天订好（平均）' },
  { key: 'seconds_to_handoff_p50', title: '从开始到订好（中位）' },
]

export const KPI_KEYS: readonly string[] = KPI_CARDS.map((card) => card.key)

/** 指标值的显示文本：比例显示百分比，其他按单位；测不出来就说测不出来，不写 0。 */
export function formatMetric(metric: MetricResult | undefined): string {
  if (!metric || metric.status !== 'measured' || metric.value === null || metric.value === undefined) {
    return '测不出来'
  }
  if (metric.unit === 'rate') return `${(metric.value * 100).toFixed(1)}%`
  if (metric.unit === 'ratio') {
    const percent = (metric.value * 100).toFixed(1)
    return `${metric.value > 0 ? '+' : ''}${percent}%`
  }
  if (metric.unit === 'seconds') return formatSeconds(metric.value)
  const rounded = Number.isInteger(metric.value) ? String(metric.value) : metric.value.toFixed(1)
  if (metric.unit === 'days') return `${rounded} 天`
  if (metric.unit === 'minutes') return `${rounded} 分钟`
  if (metric.unit === 'rounds') return `${rounded} 轮`
  if (metric.unit === 'calls') return `${rounded} 次`
  if (metric.unit === 'count') return rounded
  return metric.unit ? `${rounded} ${metric.unit}` : rounded
}

/** 秒数按人看得懂的粒度显示：不到一分钟说秒，不到一小时说分钟，再往上说小时。 */
export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)} 分钟`
  return `${(seconds / 3600).toFixed(1)} 小时`
}

/** 分母的说明文本，让人知道这个数是几个样本算出来的。 */
export function metricSample(metric: MetricResult | undefined): string {
  if (!metric || metric.status !== 'measured') return metric?.confidence_note ?? '没有可用样本'
  if (metric.denominator === null || metric.denominator === undefined) return ''
  const numerator = metric.numerator ?? 0
  return metric.unit === 'rate' ? `${numerator} / ${metric.denominator}` : `样本 ${metric.denominator}`
}

/** 预算用了几成（0–1，封顶 1）；没有账本数据时为 null。 */
export function utilisation(spent: string | null, limit: string): number | null {
  if (spent === null) return null
  const used = Number(spent)
  const cap = Number(limit)
  if (!Number.isFinite(used) || !Number.isFinite(cap) || cap <= 0) return null
  return Math.min(1, Math.max(0, used / cap))
}

/** 预算条的颜色档：八成以上警告，超了红。 */
export function utilisationTone(ratio: number | null): 'ok' | 'warn' | 'over' | 'unknown' {
  if (ratio === null) return 'unknown'
  if (ratio >= 1) return 'over'
  if (ratio >= 0.8) return 'warn'
  return 'ok'
}

export function whereaboutsLabel(status: WhereaboutsStatus): string {
  switch (status) {
    case 'IN_TRANSIT':
      return '在途'
    case 'AT_DESTINATION':
      return '在目的地'
    case 'UPCOMING':
      return '未出发'
    case 'COMPLETED':
      return '已结束'
  }
}

export function whereaboutsTone(status: WhereaboutsStatus): string {
  switch (status) {
    case 'IN_TRANSIT':
      return 'blue'
    case 'AT_DESTINATION':
      return 'ok'
    case 'UPCOMING':
      return 'neutral'
    case 'COMPLETED':
      return 'dark'
  }
}
