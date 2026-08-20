/**
 * 任务状态与展示文案工具：把后端 TaskState / PolicyOutcome 映射为中文标签。
 * 同时提供差旅日期时间的「墙钟」解析与格式化，避免被浏览器时区改写。
 */
import type { PolicyOutcome, TaskState } from '../api/types'

/** 某个任务状态对应的界面文案、语气与用户旅程阶段。 */
export interface StateMeta {
  label: string
  description: string
  tone: 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'waiting'
  /** 面向员工旅程的主阶段划分。 */
  phase:
    | 'intake'
    | 'search'
    | 'options'
    | 'approval'
    | 'handoff'
    | 'recovery'
    | 'terminal'
}

const STATE_META: Record<TaskState, StateMeta> = {
  DRAFT: {
    label: '草稿',
    description: '任务已创建，正在解析行程意图',
    tone: 'neutral',
    phase: 'intake',
  },
  NEEDS_CLARIFICATION: {
    label: '需要澄清',
    description: '缺少必填信息或存在冲突，请补充说明',
    tone: 'warning',
    phase: 'intake',
  },
  NEEDS_STRUCTURED_INPUT: {
    label: '需要表单',
    description: '自然语言轮次已用尽，请用结构化表单补全',
    tone: 'warning',
    phase: 'intake',
  },
  SEARCHING: {
    label: '搜索中',
    description: '正在查询航班/高铁与酒店库存',
    tone: 'info',
    phase: 'search',
  },
  WAITING_FOR_PROVIDER: {
    label: '等待供应商',
    description: '供应商瞬时故障，将按计划自动重试',
    tone: 'waiting',
    phase: 'recovery',
  },
  PROVIDER_FAILED: {
    label: '供应商失败',
    description: '库存查询失败，可重试或修改条件',
    tone: 'danger',
    phase: 'recovery',
  },
  PLANNING: {
    label: '规划中',
    description: '正在组合行程并做政策校验',
    tone: 'info',
    phase: 'search',
  },
  OPTIONS_READY: {
    label: '方案就绪',
    description: '方案已生成，等待进入选择阶段',
    tone: 'info',
    phase: 'options',
  },
  NO_FEASIBLE_OPTION: {
    label: '无可行方案',
    description: '当前约束下没有可行组合，请调整条件',
    tone: 'warning',
    phase: 'recovery',
  },
  WAITING_FOR_USER: {
    label: '待选择',
    description: '请选择一个合规或需审批的方案',
    tone: 'info',
    phase: 'options',
  },
  WAITING_FOR_APPROVAL: {
    label: '待审批',
    description: '例外方案等待直属经理审批',
    tone: 'waiting',
    phase: 'approval',
  },
  REVALIDATING: {
    label: '重新校验',
    description: '交接前正在重验价格与库存',
    tone: 'info',
    phase: 'handoff',
  },
  RECONFIRMATION_REQUIRED: {
    label: '需重新确认',
    description: '价格变化或售罄，请重新规划',
    tone: 'warning',
    phase: 'recovery',
  },
  READY_FOR_HANDOFF: {
    label: '可交接',
    description: '已生成官方平台交接链接（不含支付/出票）',
    tone: 'success',
    phase: 'handoff',
  },
  HANDED_OFF: {
    label: '已交接',
    description: '用户已确认前往官方平台办理',
    tone: 'success',
    phase: 'terminal',
  },
  TOOL_BUDGET_EXHAUSTED: {
    label: '工具预算耗尽',
    description: '本任务外部调用次数已达上限，流程终止',
    tone: 'danger',
    phase: 'terminal',
  },
  OUT_OF_SCOPE: {
    label: '超出范围',
    description: '请求不在差旅规划范围内',
    tone: 'danger',
    phase: 'terminal',
  },
}

/**
 * 按任务状态返回展示用元数据；未知状态回退为中性文案。
 */
export function getStateMeta(state: TaskState): StateMeta {
  return STATE_META[state] ?? {
    label: state,
    description: '',
    tone: 'neutral',
    phase: 'terminal',
  }
}

/** 将政策结论枚举转成简短中文标签。 */
export function policyLabel(outcome: PolicyOutcome): string {
  switch (outcome) {
    case 'COMPLIANT':
      return '合规'
    case 'REQUIRES_APPROVAL':
      return '需审批'
    case 'FORBIDDEN':
      return '禁止'
    case 'INSUFFICIENT_EVIDENCE':
      return '证据不足'
    default:
      return outcome
  }
}

/** 将政策结论映射为与 StateMeta 一致的语气色。 */
export function policyTone(outcome: PolicyOutcome): StateMeta['tone'] {
  switch (outcome) {
    case 'COMPLIANT':
      return 'success'
    case 'REQUIRES_APPROVAL':
      return 'warning'
    case 'FORBIDDEN':
    case 'INSUFFICIENT_EVIDENCE':
      return 'danger'
    default:
      return 'neutral'
  }
}

/** 按货币格式化金额；无法解析时退回原始文本。 */
export function formatMoney(amount: string | number, currency: string): string {
  const value = typeof amount === 'string' ? Number(amount) : amount
  if (Number.isNaN(value)) return `${amount} ${currency}`
  try {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: currency || 'USD',
      maximumFractionDigits: 2,
    }).format(value)
  } catch {
    return `${value} ${currency}`
  }
}

/** ISO 字符串中的日历/墙钟字段，不按浏览器时区换算。 */
export interface IsoWallClock {
  year: number
  month: number
  day: number
  hour: number | null
  minute: number | null
  offset: string | null
}

/**
 * 解析 ISO/日期字符串的墙钟字段，不转换到浏览器本地时区。
 */
export function parseIsoWallClock(value: string): IsoWallClock | null {
  const trimmed = value.trim()
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed)
  if (dateOnly) {
    return {
      year: Number(dateOnly[1]),
      month: Number(dateOnly[2]),
      day: Number(dateOnly[3]),
      hour: null,
      minute: null,
      offset: null,
    }
  }
  const dateTime =
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?/.exec(
      trimmed,
    )
  if (!dateTime) return null
  return {
    year: Number(dateTime[1]),
    month: Number(dateTime[2]),
    day: Number(dateTime[3]),
    hour: Number(dateTime[4]),
    minute: Number(dateTime[5]),
    offset: dateTime[6] ?? null,
  }
}

/** 将数字补零为两位字符串。 */
function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 格式化为机场/当地出发抵达时钟时间；优先使用墙钟字段。
 */
export function formatTravelTime(value: string): string {
  const parts = parseIsoWallClock(value)
  if (parts?.hour === null || parts?.minute === null || parts === null) {
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value.slice(11, 16) || value
    return new Intl.DateTimeFormat('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date)
  }
  return `${pad2(parts.hour)}:${pad2(parts.minute)}`
}

/**
 * 格式化为机场/当地日历日期（月日）。
 */
export function formatTravelDate(value: string): string {
  const parts = parseIsoWallClock(value)
  if (!parts) {
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value.slice(0, 10)
    return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(date)
  }
  return `${parts.month}月${parts.day}日`
}

/** 格式化日期时间；带非 UTC 偏移时保留墙钟显示。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const parts = parseIsoWallClock(value)
  if (parts?.offset && parts.offset !== 'Z' && parts.hour !== null && parts.minute !== null) {
    return `${parts.year}/${pad2(parts.month)}/${pad2(parts.day)} ${pad2(parts.hour)}:${pad2(parts.minute)}`
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

/** 截断过长 ID，便于界面展示。 */
export function shortId(id: string, keep = 8): string {
  if (id.length <= keep + 2) return id
  return `${id.slice(0, keep)}…`
}

/**
 * 判断任务是否处于适合轮询的中间态（搜索/规划/重验等）。
 */
export function shouldPoll(state: TaskState): boolean {
  return (
    state === 'SEARCHING' ||
    state === 'PLANNING' ||
    state === 'REVALIDATING' ||
    state === 'WAITING_FOR_PROVIDER' ||
    state === 'OPTIONS_READY'
  )
}
