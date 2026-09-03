/**
 * 后端 HTTP 客户端：统一封装 fetch、鉴权头与错误处理。
 * 对外导出 `api` 对象，供页面调用差旅任务、审批与健康检查等接口。
 */
import type {
  ActivePolicy,
  ApiErrorBody,
  AuditEvent,
  BookingConfirmationCreate,
  BudgetsResponse,
  BusinessMetricsReport,
  DutyOfCareResponse,
  HealthResponse,
  LoginResponse,
  StructuredTripCreate,
  TaskStepsResponse,
  TaskSummary,
  ProvenanceRecord,
  TripTask,
  UserIdentity,
  TripAggregate,
  TripEventCreate,
} from './types'
import type { TaskStep } from './types'
import { feedSse } from '../utils/stream'

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/** API 请求失败时抛出的错误，携带 HTTP 状态码与响应体。 */
export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/** 从 localStorage 读取当前访问令牌。 */
function getToken(): string | null {
  return localStorage.getItem('cta_access_token')
}

/**
 * 写入或清除访问令牌。
 * 传入 null 时删除本地 token。
 */
export function setToken(token: string | null): void {
  if (token) {
    localStorage.setItem('cta_access_token', token)
  } else {
    localStorage.removeItem('cta_access_token')
  }
}

/** 将后端错误 body 转成可读的提示文案。 */
function formatDetail(body: ApiErrorBody | unknown): string {
  if (!body || typeof body !== 'object') return 'Request failed'
  const detail = (body as ApiErrorBody).detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg).filter(Boolean).join('; ') || 'Validation failed'
  }
  return 'Request failed'
}

/**
 * 发起带鉴权头的 JSON 请求；401 时清 token 并广播未授权事件。
 */
async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers)
  if (!headers.has('Content-Type') && options.body) {
    headers.set('Content-Type', 'application/json')
  }
  const token = getToken()
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  })

  if (response.status === 204) {
    return undefined as T
  }

  let body: unknown = null
  const text = await response.text()
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }

  if (!response.ok) {
    if (response.status === 401 && path !== '/auth/login') {
      setToken(null)
      window.dispatchEvent(new Event('cta:unauthorized'))
    }
    throw new ApiError(response.status, formatDetail(body), body)
  }

  return body as T
}

/**
 * 读取 SSE 流：`step` 事件回调给上层，`task` 事件作为返回值，`error` 事件抛错。
 * 没降低任务总耗时——降低的是感知延迟：第一步进展 1 秒内就能上屏。
 */
async function streamTask(
  path: string,
  body: unknown,
  onStep: (step: TaskStep) => void,
): Promise<TripTask> {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  })
  if (!response.ok || !response.body) {
    let parsed: unknown = null
    try {
      parsed = await response.json()
    } catch {
      parsed = null
    }
    throw new ApiError(response.status, formatDetail(parsed), parsed)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let task: TripTask | null = null
  let failure: { type?: string; detail?: string } | null = null
  for (;;) {
    const { done, value } = await reader.read()
    const chunk = done ? '' : decoder.decode(value, { stream: true })
    const parsedChunk = feedSse(buffer, chunk)
    buffer = parsedChunk.rest
    for (const item of parsedChunk.events) {
      if (item.event === 'step') onStep(item.data as TaskStep)
      else if (item.event === 'task') task = item.data as TripTask
      else if (item.event === 'error') failure = item.data as { type?: string; detail?: string }
    }
    if (done) break
  }
  if (failure) throw new ApiError(0, failure.detail ?? failure.type ?? '任务执行失败', failure)
  if (!task) throw new ApiError(0, '流结束但没有收到任务结果')
  return task
}

/**
 * 面向业务页面的 API 方法集合。
 * 覆盖健康检查、登录、差旅任务生命周期与审批收件箱。
 */
export const api = {
  /** 查询后端健康与运行边界状态。 */
  health: () => request<HealthResponse>('/health'),

  /** 使用用户 ID 与密码登录，返回访问令牌。 */
  login: (user_id: string, password: string) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ user_id, password }),
    }),

  /** 读取当前登录用户身份。 */
  me: () => request<UserIdentity>('/auth/me'),

  /** 列出差旅任务（可按摘要、数量、状态过滤）。 */
  listTasks: (params?: { summary?: boolean; limit?: number; state?: string }) => {
    const q = new URLSearchParams()
    if (params?.summary !== undefined) q.set('summary', String(params.summary))
    if (params?.limit !== undefined) q.set('limit', String(params.limit))
    if (params?.state) q.set('state', params.state)
    const qs = q.toString()
    return request<TaskSummary[] | TripTask[]>(`/trip-tasks${qs ? `?${qs}` : ''}`)
  },

  /** 按任务 ID 获取完整差旅任务详情。 */
  getTask: (taskId: string) => request<TripTask>(`/trip-tasks/${taskId}`),

  /** 用工具循环入口创建自然语言任务：模型决定下一查，没有填表编译。 */
  createAgenticNaturalLanguage: (message: string, traveler_id: string) =>
    request<TripTask>('/agentic/trip-tasks', {
      method: 'POST',
      body: JSON.stringify({ message, traveler_id }),
    }),

  /** 用结构化字段创建新的差旅任务。 */
  createStructured: (payload: StructuredTripCreate) =>
    request<TripTask>('/trip-tasks', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 仅向工具循环任务追加消息。 */
  submitAgenticMessage: (taskId: string, message: string) =>
    request<TripTask>(`/agentic/trip-tasks/${taskId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),

  /** 在澄清轮次用尽后，提交结构化行程表单并继续搜索。 */
  submitStructuredRequest: (taskId: string, payload: StructuredTripCreate) =>
    request<TripTask>(`/trip-tasks/${taskId}/structured-request`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 选择某个候选方案；需审批时可附带业务原因。 */
  selectOption: (taskId: string, option_id: string, business_reason?: string) =>
    request<TripTask>(`/trip-tasks/${taskId}/select-option`, {
      method: 'POST',
      body: JSON.stringify({
        option_id,
        business_reason: business_reason || null,
      }),
    }),

  /** 请求后端对该任务重新规划方案。 */
  replan: (taskId: string) =>
    request<TripTask>(`/trip-tasks/${taskId}/replan`, { method: 'POST' }),

  /** 修订任务的结构化行程请求。 */
  reviseRequest: (taskId: string, payload: StructuredTripCreate) =>
    request<TripTask>(`/trip-tasks/${taskId}/revise-request`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 标记官方平台交接已完成。 */
  handoffCompleted: (taskId: string) =>
    request<TripTask>(`/trip-tasks/${taskId}/handoff-completed`, {
      method: 'POST',
    }),

  /** 员工回填订单号和实付金额（自述，不是回执）。一个任务只能填一次。 */
  confirmBooking: (taskId: string, payload: BookingConfirmationCreate) =>
    request<TripTask>(`/trip-tasks/${taskId}/booking-confirmation`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 审批人对待办任务做出批准或拒绝决定。 */
  decideApproval: (
    taskId: string,
    approved: boolean,
    reason: string,
    approver_id?: string,
  ) =>
    request<TripTask>(`/approvals/${taskId}/decision`, {
      method: 'POST',
      body: JSON.stringify({
        approved,
        reason,
        approver_id: approver_id ?? null,
      }),
    }),

  /** 拉取当前登录审批人的待办收件箱。 */
  approvalInbox: (limit = 100) =>
    request<TaskSummary[]>(`/approvals/inbox?limit=${limit}`),

  /** 读取指定任务的审计事件列表。 */
  auditEvents: (taskId: string) =>
    request<AuditEvent[]>(`/trip-tasks/${taskId}/audit-events`),

  /** 读取当前生效的差旅政策快照。 */
  activePolicy: () => request<ActivePolicy>('/policy'),

  /** 读取近期跨任务审计事件（仅管理员）。 */
  recentAuditEvents: (limit = 50) =>
    request<AuditEvent[]>(`/audit-events?limit=${limit}`),

  /** 业务结果指标（仅管理员）。 */
  businessMetrics: (limit = 200) =>
    request<BusinessMetricsReport>(`/metrics/business?limit=${limit}`),

  /** 谁在哪：确认过的行程此刻的位置（仅管理员）。 */
  dutyOfCare: (params?: { at?: string; include_completed?: boolean }) => {
    const q = new URLSearchParams()
    if (params?.at) q.set('at', params.at)
    if (params?.include_completed) q.set('include_completed', 'true')
    const qs = q.toString()
    return request<DutyOfCareResponse>(`/duty-of-care${qs ? `?${qs}` : ''}`)
  },

  /** 成本中心预算消耗（仅管理员）。 */
  budgets: () => request<BudgetsResponse>('/budgets'),

  /** 一趟差旅：观察对象、航班动态、变更事件。 */
  getTrip: (tripId: string) => request<TripAggregate>(`/trips/${tripId}`),

  /** 报一条变更事件（会议改期 / 航变），后端开一个改期任务并返回它。 */
  reportTripEvent: (tripId: string, payload: TripEventCreate) =>
    request<TripTask>(`/trips/${tripId}/events`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 取消整趟差旅：观察停止、看板不再显示；票由人去官方平台退改。 */
  cancelTrip: (tripId: string, reason?: string) =>
    request<TripAggregate>(`/trips/${tripId}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason?.trim() || null }),
    }),

  /**
   * 流式创建自然语言任务：`onStep` 逐步收到过程记录，返回值是最终任务。
   * 后端不支持流式（404/405）时抛 ApiError，调用方退回非流式接口。
   */
  createAgenticStream: (
    message: string,
    traveler_id: string,
    onStep: (step: TaskStep) => void,
  ) => streamTask('/agentic/trip-tasks/stream', { message, traveler_id }, onStep),

  /** 流式跟进消息；语义同上，作用在已有任务上。 */
  submitAgenticMessageStream: (
    taskId: string,
    message: string,
    onStep: (step: TaskStep) => void,
  ) => streamTask(`/agentic/trip-tasks/${taskId}/messages/stream`, { message }, onStep),

  /** 任务从建到现在的每一步，后端按先后整理好。 */
  taskSteps: (taskId: string) =>
    request<TaskStepsResponse>(`/trip-tasks/${taskId}/steps`),

  /** 一条方案的依据链：每一步凭什么，以及哪些地方说不出来（gaps）。 */
  optionProvenance: (taskId: string, optionId: string) =>
    request<ProvenanceRecord>(`/trip-tasks/${taskId}/options/${optionId}/provenance`),

  /** 重算依据链并和交接时钉住的指纹比对。 */
  provenanceCheck: (taskId: string) =>
    request<Record<string, unknown>>(`/trip-tasks/${taskId}/provenance-check`),
}
