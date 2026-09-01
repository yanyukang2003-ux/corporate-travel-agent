/** 管理看板的纯函数：指标怎么显示、预算用了几成、位置状态叫什么。 */

import type { MetricResult, WhereaboutsStatus } from '../api/types'

/** 看板首屏放哪几个指标、按什么顺序。其余指标在下方全表里。 */
export const KPI_KEYS = [
  'booking_confirmation_rate',
  'approval_request_rate',
  'off_channel_expense_rate',
  'change_intervention_rate',
  'advance_booking_days_mean',
  'time_to_handoff_minutes_p50',
] as const

/** 指标值的显示文本：比例显示百分比，其他按单位；测不出来就说测不出来，不写 0。 */
export function formatMetric(metric: MetricResult | undefined): string {
  if (!metric || metric.status !== 'measured' || metric.value === null || metric.value === undefined) {
    return '测不出来'
  }
  if (metric.unit === 'rate') return `${(metric.value * 100).toFixed(1)}%`
  const rounded = Number.isInteger(metric.value) ? String(metric.value) : metric.value.toFixed(1)
  if (metric.unit === 'days') return `${rounded} 天`
  if (metric.unit === 'minutes') return `${rounded} 分钟`
  if (metric.unit === 'count') return rounded
  return metric.unit ? `${rounded} ${metric.unit}` : rounded
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
