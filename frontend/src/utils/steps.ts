/**
 * 「过程记录」的展示整理：把后端 `GET /trip-tasks/{id}/steps` 给的步骤翻成
 * 时间线上的一行行。后端负责收集和排序，这里只负责"怎么说给人看"。
 */

import type { TaskStep } from '../api/types'

/** 时间线上的一步。 */
export interface StepView {
  key: string
  time: string
  title: string
  /** ok=正常，warn=失败/异常，info=里程碑说明，muted=辅助信息。 */
  tone: 'ok' | 'warn' | 'info' | 'muted'
  lines: string[]
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

/** 步骤时间：只显示时分，日期变化靠上下文；无时间戳显示占位。 */
export function stepTime(at: string | null): string {
  if (!at) return '—'
  const date = new Date(at)
  if (Number.isNaN(date.getTime())) return at.slice(11, 16) || at
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

/** 失败与异常标 warn；里程碑标 info；其余按正常步骤展示。 */
export function stepTone(step: TaskStep): StepView['tone'] {
  const status = (step.status ?? '').toUpperCase()
  if (status.includes('FAIL') || status.includes('ERROR')) return 'warn'
  const kind = step.kind
  if (kind === 'milestone') {
    const type = asText(step.detail.event_type)
    if (type.includes('FAILED') || type.includes('ABORTED') || type.includes('EXHAUSTED')
      || type.includes('NO_FEASIBLE') || type.includes('INVALIDATED') || type.includes('REJECTED')) {
      return 'warn'
    }
    return 'info'
  }
  if (kind === 'message') return 'muted'
  return 'ok'
}

/** 每一步的补充行：这一步的输出内容，按种类说人话。 */
export function stepLines(step: TaskStep): string[] {
  const d = step.detail
  switch (step.kind) {
    case 'message':
      return [asText(d.content)]
    case 'tool_call': {
      const parts = [asText(d.tool_kind), asText(step.status)]
      if (d.duration_ms !== undefined) parts.push(`${asText(d.duration_ms)}ms`)
      if (d.retry_of !== undefined) parts.push(`重试第 ${asText(d.retry_of)} 次调用`)
      if (d.error_code) parts.push(`错误 ${asText(d.error_code)}`)
      return [parts.filter(Boolean).join(' · ')]
    }
    case 'search': {
      const lines: string[] = []
      if (d.date_evidence) lines.push(`出处：「${asText(d.date_evidence)}」`)
      if (d.assumption) lines.push(`假设：${asText(d.assumption)}`)
      const count = d.result_count
      lines.push(count === null || count === undefined ? '结果数未知' : `结果 ${asText(count)} 条`)
      for (const sample of asArray(d.samples).slice(0, 3)) {
        const item = sample as Record<string, unknown>
        lines.push(`· ${asText(item.label)} · ${asText(item.price)} ${asText(item.currency)}`)
      }
      return lines
    }
    case 'plan': {
      const lines = asArray(d.options).map((option) => {
        const item = option as Record<string, unknown>
        return `· ${asText(item.route)} · ${asText(item.total_cost)} ${asText(item.currency)} · ${policyText(asText(item.policy_outcome))}`
      })
      for (const question of asArray(d.open_questions)) lines.push(`还没定：${asText(question)}`)
      return lines
    }
    case 'approval': {
      const lines = [`审批人 ${asText(d.approver_id)} · ${asText(d.status)}`]
      if (d.business_reason) lines.push(`业务原因：${asText(d.business_reason)}`)
      if (asArray(d.violations).length > 0) lines.push(`在批：${asArray(d.violations).map(asText).join('、')}`)
      if (d.decision_reason) lines.push(`决定说明：${asText(d.decision_reason)}`)
      return lines
    }
    case 'confirmation': {
      const refs = asArray(d.order_references).map(asText).join('、')
      return [
        refs ? `订单号 ${refs}` : '订单号未填',
        `实付 ${asText(d.total_amount)} ${asText(d.currency)} · ${asText(d.reported_by)} 回填`,
      ]
    }
    default: {
      // handoff / reconciliation / milestone：把有值的字段摆出来，不编内容。
      return Object.entries(d)
        .filter(([key, value]) => value !== null && value !== undefined && key !== 'event_type'
          && asText(value) !== '' && asArray(value).length !== 0 || Array.isArray(value) && value.length > 0)
        .slice(0, 4)
        .map(([key, value]) => `${key}：${Array.isArray(value) ? value.map(asText).join('、') : asText(value)}`)
    }
  }
}

function policyText(outcome: string): string {
  if (outcome === 'COMPLIANT') return '合规'
  if (outcome === 'REQUIRES_APPROVAL') return '需审批'
  if (outcome === 'FORBIDDEN') return '禁止'
  if (outcome === 'INSUFFICIENT_EVIDENCE') return '判不了'
  return outcome
}

/** 把后端步骤翻成时间线视图；顺序照后端，不重排。 */
export function stepViews(steps: TaskStep[]): StepView[] {
  return steps.map((step) => ({
    key: `${step.sequence}-${step.kind}`,
    time: stepTime(step.at),
    title: step.title,
    tone: stepTone(step),
    lines: stepLines(step).filter(Boolean),
  }))
}
