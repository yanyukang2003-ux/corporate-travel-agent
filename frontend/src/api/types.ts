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
  | 'BOOKING_CONFIRMED'
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

/** 本次请求包含一个去程航段、一个返程航段，或完整往返。 */
export type BookingScope = 'OUTBOUND_ONLY' | 'RETURN_ONLY' | 'ROUND_TRIP'

/** 当前登录用户的身份与角色信息。 */
export interface UserIdentity {
  user_id: string
  roles: Role[]
  employee_id: string | null
  /** 我可以替谁订差旅（别人把我列进了委托名单）。 */
  can_book_for?: string[]
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
  /**
   * 阈值本来就是数字的规则（今天只有酒店夜费上限）才有这三个字段和 overage_amount。
   * 其余规则全是 null——那是"这条规则谈不上差额"，不是"差额为零"。
   *
   * **不要去 parse actual / threshold 那两个字符串。** 它们是展示文本，
   * 措辞一改这里的数字就悄悄变了。
   */
  actual_amount: string | null
  threshold_amount: string | null
  amount_currency: string | null
  /** 超出阈值多少。没有数值阈值时为 null。 */
  overage_amount: string | null
}

/** 一条规则超出了多少。单位见 unit——夜费上限是每晚，不是整趟。 */
export interface PolicyOverage {
  rule_id: string
  amount: string
  unit: string
  currency: string
  exception_allowed: boolean
}

/** 换成另一条方案会省多少、代价是什么。省钱和代价永远一起出现。 */
export interface Tradeoff {
  option_id: string
  /** 省多少钱，恒为正。 */
  saves: string
  /** 出发晚多少分钟；负数是更早走。 */
  departure_delta_minutes: number
  /** 路上多花多少分钟；负数是更快。 */
  duration_delta_minutes: number
  currency: string
}

/** 选这条方案要付出什么。由后端确定性计算，不查库存、不调模型。 */
export interface CostGuidance {
  option_id: string
  currency: string
  /** 比这批里最便宜的合规方案贵多少。没有合规基准或币种不一致时为 null。 */
  premium_over_cheapest_compliant: string | null
  cheapest_compliant_option_id: string | null
  policy_overages: PolicyOverage[]
  /** 选它要谁批；只有需审批时才有值。 */
  approver_id: string | null
  tradeoffs: Tradeoff[]
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
  /** 这一段属于哪张票。null = 分段购买；同一个值的几段是一张不可拆的整票。 */
  fare_ref: string | null
}

/** 后端按真实方向生成的可执行交通航段。 */
export interface TripLeg {
  role: 'OUTBOUND' | 'RETURN'
  origin: string
  destination: string
  depart_after: string
  arrive_before: string
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
  legs: TransportOffer[]
  /** 这条方案要买几张票、各多少钱。展示价格读这里，不要逐段读 price。 */
  fares: { fare_ref: string | null; total: string | number }[]
  outbound: TransportOffer
  inbound: TransportOffer | null
  stays: HotelOffer[]
  hotel: HotelOffer | null
  total_cost: string | number
  total_duration_minutes: number
  currency: string
  feasibility: FeasibilityResult
  preference_penalty: string | number
  score: string | number
  policy_outcome: PolicyOutcome
  rule_evidence: RuleEvidence[]
  cost_guidance: CostGuidance
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
  /** 审批链：第一级永远是直属经理，之后按政策分级追加；空数组是旧任务，只有 approver_id 一级。 */
  steps: ApprovalStep[]
  current_step: number
}

/** 分级审批里的一级。 */
export interface ApprovalStep {
  approver_id: string
  label: string
  status: 'PENDING' | 'APPROVED' | 'REJECTED' | 'INVALIDATED'
  decided_at: string | null
  reason: string | null
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

/** 一条下单确认是谁说的。今天只有员工自述这一档；费控对账接上时再加。 */
export type BookingConfirmationSource = 'SELF_REPORTED'

/**
 * 员工回填的下单确认：订单号、实付金额。**自述，不是回执**——系统核不了订单号。
 * 方案价与差额由后端读时现算；找不到方案时 planned_total 为 null，币种不一致时 cost_variance 为 null。
 */
export interface BookingConfirmation {
  confirmation_id: string
  intent_id: string
  option_id: string
  option_version: number
  order_references: string[]
  total_amount: string | number
  currency: string
  source: BookingConfirmationSource
  reported_by: string
  reported_at: string
  booked_at: string
  note: string | null
  planned_total: string | number | null
  planned_currency: string | null
  cost_variance: string | number | null
}

/** 费控系统的记录和这趟任务的下单确认对上了。 */
export interface ExpenseReconciliation {
  reconciliation_id: string
  expense_id: string
  source_system: string
  expense_amount: string | number
  currency: string
  expensed_at: string
  reconciled_at: string
  status: 'MATCHED' | 'AMOUNT_MISMATCH' | 'CURRENCY_MISMATCH'
  matched_order_references: string[]
  note: string | null
  /** 费控金额减自述金额；币种不同为 null。 */
  amount_variance: string | number | null
}

/** 回填订单号的请求体。金额用字符串传，避免浮点。 */
export interface BookingConfirmationCreate {
  order_references: string[]
  total_amount: string
  currency: string
  booked_at?: string | null
  note?: string | null
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
  /** 现在轮到谁批；不在等审批时为 null。 */
  pending_approver_id?: string | null
  /** 谁发起的；旧任务等于旅行者。 */
  requester_id?: string | null
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
  input_kind?: 'date' | 'time_range' | ''
}

/**
 * 工具循环写给旅行者的那段话：推荐理由 + 还没定的事。
 * 它不在 `messages` 里（进了对话就等于改了下一轮喂给模型的输入），
 * 聊天视图要靠它，助手才有话说。
 */
export interface AgenticProposal {
  summary: string
  transport_refs: string[]
  hotel_refs: string[]
  open_questions: string[]
}

/**
 * 排序是怎么算的：把时长折算成价格的那个比例，以及此刻生效的整程偏好。
 * 分数本身在每条方案的 `score` 上，这里给的是"分数怎么来的"。
 */
export interface ScoringModel {
  /** 多少分钟折 1 个货币单位。默认 10；"怎么便宜怎么来"=60，"越快越好"=1。 */
  minutes_per_unit: string | number
  journey_preferences: string[]
}

/** 完整差旅任务详情（summary=false）。 */
export interface TripTask {
  task_id: string
  intent_entrypoint: 'structured' | 'legacy' | 'semantic' | 'agentic'
  state: TaskState
  request_version: number | null
  booking_scope: BookingScope | null
  transport_legs: TripLeg[]
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
  agentic_proposal?: AgenticProposal | null
  scoring?: ScoringModel | null
  original_instruction?: string | null
  extract_failure?: ExtractFailure | null
  model_fallback?: ModelFallback | null
  options: TravelOption[]
  selected_option_id: string | null
  approval: ApprovalInfo | null
  booking_intent: BookingIntent | null
  booking_confirmation: BookingConfirmation | null
  /** 费控对账结果；对上了，自述才算"核实过"。 */
  expense_reconciliation: ExpenseReconciliation | null
  budget_snapshot: BudgetSnapshot | null
  /** 旅行者：差标、审批、预算都看这个人。 */
  traveler_id: string
  /** 发起人：代订时和旅行者不是同一个人。 */
  requester_id: string
  is_delegated: boolean
  /** 属于哪趟差旅；旧任务为 null。 */
  trip_id: string | null
  /** 改期任务记它改的是哪个任务；规划任务为 null。 */
  parent_task_id: string | null
  change_event_id: string | null
  is_change_task: boolean
  change_event: { event_type: string; ref_id: string | null; note: string | null; excluded_refs?: string[] } | null
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

/** 某职级允许的舱等/席别。 */
export interface PolicyLevelRule {
  level: string
  allowed_flight_classes: string[]
  allowed_train_classes: string[]
}

/** 某城市的酒店每晚上限。 */
export interface PolicyHotelCap {
  city: string
  nightly_cap: string
}

/** `GET /policy` 返回的当前生效政策快照。 */
export interface ActivePolicy {
  snapshot_id: string
  policy_version: string
  content_hash: string
  currency: string
  effective_from: string
  effective_to: string | null
  arrival_buffer_minutes: number
  viewer_level: string | null
  level_rules: PolicyLevelRule[]
  hotel_city_caps: PolicyHotelCap[]
  exception_allowed_rule_ids: string[]
  /** 至少提前几天订；null 是这版政策没有这条规则。 */
  min_advance_booking_days: number | null
  /** 淡旺季夜费上限：入住日落在窗口内就替代基础上限。 */
  hotel_seasonal_caps: PolicySeasonalCap[]
  /** 成本中心预算上限；用掉多少见任务上的 budget_snapshot。 */
  cost_center_budgets: PolicyCostCenterBudget[]
}

export interface PolicySeasonalCap {
  city: string
  label: string
  season_from: string
  season_to: string
  nightly_cap: string
}

export interface PolicyCostCenterBudget {
  cost_center: string
  amount: string
  currency: string
  period_from: string
  period_to: string
}

/** 规划那一刻钉住的预算余额快照；没接账本、没成本中心或政策没配预算时任务上是 null。 */
export interface BudgetSnapshot {
  snapshot_id: string
  cost_center: string
  currency: string
  limit: string
  spent: string
  period_from: string
  period_to: string
  computed_at: string
  source: string
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
  booking_scope?: BookingScope | null
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

/** 一个业务指标：`measured` 才有值；分母为零一律 `unavailable`，不写 0。 */
export interface MetricResult {
  status: 'measured' | 'unavailable' | 'not_applicable' | string
  value: number | null
  numerator: number | null
  denominator: number | null
  unit: string | null
  exposure_note?: string | null
  confidence_note?: string | null
}

/** `GET /metrics/business`：最近任务的业务结果指标。 */
export interface BusinessMetricsReport {
  generated_at: string
  task_count: number
  expense_records_imported: number
  metrics: Record<string, MetricResult>
  labels: Record<string, string>
}

export type WhereaboutsStatus = 'UPCOMING' | 'IN_TRANSIT' | 'AT_DESTINATION' | 'COMPLETED'

export interface WatchLeg {
  ref_id: string
  provider: string
  origin: string
  destination: string
  depart_at: string
  arrive_at: string
}

/** 一位旅行者此刻在哪——只来自确认过的行程。 */
export interface TravelerWhereabouts {
  trip_id: string
  task_id: string
  traveler_id: string
  requester_id: string
  status: WhereaboutsStatus
  location: string
  current_leg: WatchLeg | null
  next_leg: WatchLeg | null
  trip_status: string
  change_pending: boolean
  watch_until: string
}

export interface DutyOfCareResponse {
  at: string
  travelers: TravelerWhereabouts[]
}

/** 一个成本中心的预算行：额度、账本支出、已交接未确认的在途金额。 */
export interface BudgetLine {
  cost_center: string
  currency: string
  limit: string
  period_from: string
  period_to: string
  spent: string | null
  committed: string
  remaining: string | null
  ledger_available: boolean
}

export interface BudgetsResponse {
  policy_snapshot_id: string
  budgets: BudgetLine[]
}

/** 过程记录中的一步：后端已按先后排好，内容全部来自落库记录。 */
export interface TaskStep {
  sequence: number
  at: string | null
  kind: string
  title: string
  status: string | null
  detail: Record<string, unknown>
}

/** `GET /trip-tasks/{id}/steps` 的返回。 */
export interface TaskStepsResponse {
  task_id: string
  count: number
  steps: TaskStep[]
}

/** 一条方案的依据链；`gaps` 是说不出依据的地方，后端不许它静默省略。 */
export interface ProvenanceRecord {
  option_id?: string
  gaps?: string[]
  [key: string]: unknown
}
