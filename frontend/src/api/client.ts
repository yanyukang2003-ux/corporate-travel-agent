/**
 * 后端 HTTP 客户端：统一封装 fetch、鉴权头与错误处理。
 * 对外导出 `api` 对象，供页面调用差旅任务、审批与健康检查等接口。
 */
import type {
  ActivePolicy,
  ApiErrorBody,
  AuditEvent,
  BookingConfirmationCreate,
  HealthResponse,
  LoginResponse,
  StructuredTripCreate,
  TaskSummary,
  TripTask,
  UserIdentity,
} from './types'

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

  /** 用保留的旧字段链路创建自然语言任务。 */
  createLegacyNaturalLanguage: (message: string, traveler_id: string) =>
    request<TripTask>('/legacy/trip-tasks', {
      method: 'POST',
      body: JSON.stringify({ message, traveler_id }),
    }),

  /** 用完整对话语义链路创建自然语言任务。 */
  createSemanticNaturalLanguage: (message: string, traveler_id: string) =>
    request<TripTask>('/semantic/trip-tasks', {
      method: 'POST',
      body: JSON.stringify({ message, traveler_id }),
    }),

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

  /** 仅向旧链路任务追加消息。 */
  submitLegacyMessage: (taskId: string, message: string) =>
    request<TripTask>(`/legacy/trip-tasks/${taskId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),

  /** 仅向新语义任务追加消息。 */
  submitSemanticMessage: (taskId: string, message: string) =>
    request<TripTask>(`/semantic/trip-tasks/${taskId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message }),
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
}
