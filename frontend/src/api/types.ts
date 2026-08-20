/**
 * 与后端 public API 载荷对齐的 TypeScript 类型定义。
 * 供前端客户端、页面组件与工具函数共享，避免字段漂移。
 */

/** 差旅任务在后端状态机中的全部可能状态。 */
export type TaskState =
  | 'DRAFT'
  | 'NEEDS_CLARIFICATION'
  | 'NEEDS_STRUCTURED_INPUT'
  | 'SEARCHING'
  | 'WAITING_FOR_PROVIDER'
  | 'PROVIDER_FAILED'
  | 'PLANNING'
  | 'OPTIONS_READY'
  | 'NO_FEASIBLE_OPTION'
  | 'WAITING_FOR_USER'
  | 'WAITING_FOR_APPROVAL'
  | 'REVALIDATING'
  | 'RECONFIRMATION_REQUIRED'
  | 'READY_FOR_HANDOFF'
  | 'HANDED_OFF'
  | 'TOOL_BUDGET_EXHAUSTED'
  | 'OUT_OF_SCOPE'

/** 政策引擎对单个方案给出的合规结论。 */
export type PolicyOutcome =
  | 'COMPLIANT'
  | 'REQUIRES_APPROVAL'
  | 'FORBIDDEN'
  | 'INSUFFICIENT_EVIDENCE'

/** 前端工作台可用的用户角色。 */
export type Role = 'employee' | 'approver' | 'admin'

/** 当前登录用户的身份与角色信息。 */
export interface UserIdentity {
  user_id: string
  roles: Role[]
  employee_id: string | null
}

/** 登录成功后返回的访问令牌载荷。 */
export interface LoginResponse {
  access_token: string
  token_type: string
  expires_at: string
}

/** `/health` 接口返回的服务健康与能力边界。 */
export interface HealthResponse {
  status: string
  booking_capability: string
  language_model: string
  language_model_status?: string
  language_model_ready?: boolean
  language_model_fallback?: string | null
  travel_provider: string
  travel_provider_mode: string
  persistence: string
  authentication: string
  active_policy_snapshot: string
  policy_config_version: string
}

/** 意图抽取失败时的分类信息（额度、鉴权、解析等）。 */
export interface ExtractFailure {
  class: 'billing' | 'auth' | 'rate_limit' | 'capacity' | 'transport' | 'parse' | 'unknown' | string
  error_code?: string
  http_status?: number | null
}

/** 模型降级/回退时的记录。 */
export interface ModelFallback {
  original_model?: string | null
  fallback_model?: string
  reason?: string
}

/** 单条政策规则的校验证据。 */
export interface RuleEvidence {
  rule_id: string
  actual: string
  threshold: string
  policy_version: string
  outcome: PolicyOutcome
  message: string
  exception_allowed: boolean
}

/** 航班或高铁等交通报价。 */
export interface TransportOffer {
  ref_id: string
  snapshot_id: string
  provider: string
  mode: 'FLIGHT' | 'TRAIN'
  origin: string
  destination: string
  depart_at: string
  arrive_at: string
  price: string | number
  seat_class: string
  available: boolean
  is_direct: boolean
  currency: string
}

/** 酒店报价及通勤相关字段。 */
export interface HotelOffer {
  ref_id: string
  snapshot_id: string
  provider: string
  name: string
  city: string
  check_in: string
  check_out: string
  nightly_price: string | number
  nights: number
  total_price: string | number
  commute_minutes: number
  commute_known?: boolean
  available: boolean
  currency: string
}

/** 任务对话中的一条用户或助手消息。 */
export interface ConversationMessage {
  role: 'user' | 'assistant' | string
  content: string
  created_at: string
}

/** 方案可行性判定结果。 */
export interface FeasibilityResult {
  feasible: boolean
  reasons: string[]
}

/** 后端返回的一条完整差旅候选方案。 */
export interface TravelOption {
  option_id: string
  version: number
  trip_request_version: number
  inventory_snapshot_ids: string[]
  inventory_refs: string[]
  outbound: TransportOffer
  inbound: TransportOffer | null
  hotel: HotelOffer | null
  total_cost: string | number
  total_duration_minutes: number
  currency: string
  feasibility: FeasibilityResult
  preference_penalty: string | number
  score: string | number
  policy_outcome: PolicyOutcome
  rule_evidence: RuleEvidence[]
  facts: string[]
}

/** 工具调用预算中的单次调用记录。 */
export interface ToolCallRecord {
  sequence: number
  tool_name: string
  tool_kind: string
  status: string
  started_at: string
  completed_at: string | null
  error_type: string | null
  error_code: string | null
  reason_code: string | null
  side_effect_class: string | null
  recovery_action: string | null
}

/** 任务级外部工具调用预算与历史。 */
export interface ToolBudget {
  limit: number
  used: number
  remaining: number
  blocked: boolean
  calls: ToolCallRecord[]
}

/** 例外审批请求及其决策状态。 */
export interface ApprovalInfo {
  approval_id: string
  subject_hash: string
  option_id: string
  option_version: number
  trip_request_version: number
  policy_snapshot_id: string
  employee_snapshot_id: string
  violations: string[]
  business_reason: string
  approver_id: string
  approved_price: string | number
  created_at: string
  expires_at: string
  status: 'PENDING' | 'APPROVED' | 'REJECTED' | 'INVALIDATED'
  decision_reason: string | null
}

/** 官方预订平台交接信息（不含本系统支付）。 */
export interface ProviderHandoff {
  provider: string
  url_or_instructions: string
  expires_at: string
}

/** 选定方案后生成的交接意图。 */
export interface BookingIntent {
  intent_id: string
  idempotency_key: string
  selected_option_id: string
  selected_option_version: number
  handoff: ProviderHandoff
  revalidated_at: string
  status: string
}

/** 供应商重试调度相关字段。 */
export interface ProviderRetry {
  next_retry_at?: string | null
  delayed_retry_count?: number | null
  [key: string]: unknown
}

/** 任务列表中的摘要视图（summary=true）。 */
export interface TaskSummary {
  task_id: string
  state: TaskState
  employee_id: string
  manager_id: string
  policy_snapshot_id: string
  request_version: number | null
  selected_option_id: string | null
  clarification_rounds: number
  option_count: number
  failure: string | null
  provider_retry: ProviderRetry
  updated_at: string
  summary: true
}

/** 澄清问题中的一个快捷选项。 */
export interface ClarificationOption {
  label: string
  description: string
  value: string
}

/** 后端下发的结构化澄清问题。 */
export interface ClarificationQuestion {
  id: string
  header: string
  question: string
  options: ClarificationOption[]
  multi_select: boolean
  slots: string[]
}

/** 完整差旅任务详情（summary=false）。 */
export interface TripTask {
  task_id: string
  state: TaskState
  request_version: number | null
  failure: string | null
  failure_details: string[] | unknown
  provider_retry: ProviderRetry
  coverage_notices: string[] | unknown
  intent_fields: Record<string, unknown>
  missing_required_fields: string[]
  conflicts: string[]
  assumptions: string[]
  clarification_question: string | null
  clarification_questions: ClarificationQuestion[]
  uncertain_slots: string[]
  clarification_rounds: number
  manipulation_detected: boolean
  tool_budget: ToolBudget
  messages?: ConversationMessage[]
  original_instruction?: string | null
  extract_failure?: ExtractFailure | null
  model_fallback?: ModelFallback | null
  options: TravelOption[]
  selected_option_id: string | null
  approval: ApprovalInfo | null
  booking_intent: BookingIntent | null
  summary: false
}

/** 任务审计事件条目。 */
export interface AuditEvent {
  event_id: string
  task_id: string
  event_type: string
  actor_type: string
  input_hash: string
  output_hash: string
  evidence_refs: string[]
  created_at: string
}

/** 结构化创建时可用的硬约束枚举。 */
export type HardConstraint =
  | 'arrive_before_meeting'
  | 'direct_only'
  | 'train_only'
  | 'flight_only'
  | 'hotel_required'

/** 结构化创建时可用的软偏好枚举。 */
export type SoftPreference =
  | 'avoid_early_departure'
  | 'hotel_near_client'
  | 'prefer_train'
  | 'prefer_flight'
  | 'compare_train_and_flight'
  | 'lowest_cost'
  | 'shortest_duration'

/** 住宿是否必需的意图取值。 */
export type LodgingRequirement = 'REQUIRED' | 'NOT_REQUIRED' | 'UNSPECIFIED'

/** 创建/修订差旅任务时提交的结构化字段载荷。 */
export interface StructuredTripCreate {
  traveler_id: string
  origin: string
  destination: string
  departure_after: string
  arrive_by: string
  return_after?: string | null
  return_before?: string | null
  hotel_check_in?: string | null
  hotel_check_out?: string | null
  hard_constraints?: HardConstraint[]
  soft_preferences?: SoftPreference[]
}

/** FastAPI 校验/业务错误响应体形状。 */
export interface ApiErrorBody {
  detail?: string | { msg: string }[]
}
