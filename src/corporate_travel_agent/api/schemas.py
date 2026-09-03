"""HTTP 契约：请求体与响应模型。

响应模型和 `serializers.py` 里的字典一一对应——每个键在这里都有一个字段，`tests/test_api_schemas.py`
会把两边的键集合对齐；漏一个键，OpenAPI 文档就少一个字段，测试会报出来。

金额在领域层是 Decimal。这里凡是此前以数字发出去的金额都标成 `float`（Pydantic 序列化 Decimal
默认成字符串，那会改线格式），此前就以字符串发出去的照旧标 `str`。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corporate_travel_agent.domain.constraints import HardConstraint, SoftPreference
from corporate_travel_agent.domain.enums import (
    ApprovalStatus,
    BookingConfirmationSource,
    BookingScope,
    ChangeImpactVerdict,
    FlightStatusKind,
    PolicyOutcome,
    ReconciliationStatus,
    SourceType,
    TaskState,
    ToolCallStatus,
    TransportMode,
    TripEventType,
    TripLegRole,
    TripStatus,
)
from corporate_travel_agent.domain.validation import validate_trip_request_values

# ============================================================================ 请求体


class TripCreate(BaseModel):
    """结构化建任务请求体。"""

    model_config = ConfigDict(extra="forbid")

    traveler_id: str = "E1001"
    booking_scope: BookingScope | None = None
    origin: str
    destination: str
    departure_after: datetime
    arrive_by: datetime
    return_after: datetime | None = None
    return_before: datetime | None = None
    hotel_check_in: date | None = None
    hotel_check_out: date | None = None
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    soft_preferences: list[SoftPreference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_windows(self) -> TripCreate:
        validate_trip_request_values(self.model_dump()).require_valid()
        return self


class NaturalLanguageTripCreate(BaseModel):
    """自然语言建任务请求体。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    traveler_id: str = "E1001"


class MessageCreate(BaseModel):
    """提交跟进消息请求体。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


class OptionSelection(BaseModel):
    """选中方案请求体。"""

    option_id: str
    business_reason: str | None = None


class ApprovalDecision(BaseModel):
    """审批决策请求体。"""

    approver_id: str | None = None
    approved: bool
    reason: str = Field(min_length=1, max_length=2000)


class BookingConfirmationCreate(BaseModel):
    """员工回填下单确认：订单号、实付金额、币种，可选下单时刻和备注。只限形状，不核真伪。"""

    model_config = ConfigDict(extra="forbid")

    order_references: list[str] = Field(min_length=1, max_length=10)
    total_amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    booked_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)


class LoginRequest(BaseModel):
    """登录请求体。"""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class TripEventRequest(BaseModel):
    """外部变更事件：航变（航司/供应商推送）或会议改期（日历/旅行者报）。"""

    model_config = ConfigDict(extra="forbid")

    event_type: TripEventType
    ref_id: str | None = Field(default=None, max_length=128)
    new_depart_at: datetime | None = None
    new_arrive_by: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    #: 会议改期改的是第几段的到场时限（0 起）；不填按第一段。
    leg_index: int | None = Field(default=None, ge=0, le=20)


class FlightStatusIn(BaseModel):
    """一条航班动态：某张票现在怎么样了。只是事实，影响由系统算。"""

    model_config = ConfigDict(extra="forbid")

    ref_id: str = Field(min_length=1, max_length=128)
    status: FlightStatusKind
    estimated_depart_at: datetime | None = None
    estimated_arrive_at: datetime | None = None
    observed_at: datetime | None = None
    source: str = Field(default="carrier-feed", min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)


class FlightStatusPush(FlightStatusIn):
    """入站推送：可以不带 trip_id，按票号找到正盯着它的差旅。"""

    trip_id: str | None = Field(default=None, max_length=64)


class TripCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class ExpenseImportRequest(BaseModel):
    """费控系统推来的一批报销记录。每条至少要有订单号，那是对账的钥匙。"""

    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(default="expense-system", min_length=1, max_length=64)
    records: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class OutboxDispatchRequest(BaseModel):
    """手动跑一轮投递的请求体。"""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=500)


# ============================================================================ 响应模型


class Out(BaseModel):
    """响应模型基类：不许漏字段，也不许多字段——字典里多出来的键会被 Pydantic 报出来。"""

    model_config = ConfigDict(extra="forbid")


class LoginResponse(Out):
    access_token: str
    token_type: str
    expires_at: datetime


class MeResponse(Out):
    user_id: str
    roles: list[str]
    employee_id: str | None
    #: 我可以替谁订：出现在别人委托名单上的那些人。
    can_book_for: list[str]


class HealthResponse(Out):
    status: str
    booking_capability: str
    language_model: str
    tool_calling_language_model: str
    intent_entrypoints: dict[str, str]
    language_model_status: str
    language_model_ready: bool
    language_model_fallback: Any
    travel_provider: str
    travel_provider_mode: str
    persistence: str
    persistence_details: dict[str, Any]
    outbox: dict[str, Any]
    raw_response_store: str
    authentication: str
    policy_config: str
    policy_config_version: str
    active_policy_snapshot: str
    policy_config_sha256: str
    provider_resilience: dict[str, Any]
    trip_watch: dict[str, Any]


# -- 任务 --------------------------------------------------------------------


class ProviderRetrySummary(Out):
    next_retry_at: datetime | None
    delayed_retry_count: int


class TaskSummaryResponse(Out):
    task_id: str
    state: str
    employee_id: str
    manager_id: str
    policy_snapshot_id: str
    request_version: int | None
    selected_option_id: str | None
    clarification_rounds: int
    option_count: int
    failure: str | None
    provider_retry: ProviderRetrySummary
    updated_at: datetime | None
    pending_approver_id: str | None
    requester_id: str | None
    summary: Literal[True]


class CommitmentOut(Out):
    place: str
    not_later_than: datetime
    purpose: str | None
    safety_buffer_required: bool


class TripLegOut(Out):
    role: TripLegRole
    origin: str
    destination: str
    depart_after: datetime
    arrive_before: datetime


class ToolCallOut(Out):
    sequence: int
    tool_name: str
    tool_kind: str
    status: ToolCallStatus
    started_at: datetime
    completed_at: datetime | None
    error_type: str | None
    error_code: str | None
    error_layer: str | None
    retryable: bool | None
    retry_of: int | None
    reason_code: str | None
    side_effect_class: str | None
    recovery_action: str | None
    counts_toward_budget: bool


class ToolBudgetOut(Out):
    limit: int
    used: int
    remaining: int
    blocked: bool
    calls: list[ToolCallOut]


class MessageOut(Out):
    role: str
    content: str
    created_at: datetime


class ScoringOut(Out):
    minutes_per_unit: float
    journey_preferences: list[str]


class TransportOfferOut(Out):
    ref_id: str
    snapshot_id: str
    provider: str
    mode: TransportMode
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    price: float
    seat_class: str
    available: bool
    is_direct: bool
    currency: str
    fare_ref: str | None


class HotelOfferOut(Out):
    ref_id: str
    snapshot_id: str
    provider: str
    name: str
    city: str
    check_in: date
    check_out: date
    nightly_price: float
    nights: int
    total_price: float
    commute_minutes: int | None
    commute_known: bool
    available: bool
    currency: str


class FareOut(Out):
    fare_ref: str | None
    total: float


class FeasibilityOut(Out):
    feasible: bool
    reasons: list[str]


class RuleEvidenceOut(Out):
    rule_id: str
    actual: str
    threshold: str
    policy_version: str
    outcome: PolicyOutcome
    message: str
    exception_allowed: bool
    actual_amount: float | None
    threshold_amount: float | None
    amount_currency: str | None
    #: 超出差标多少：确定性代码算的，不让客户端去 parse 展示字符串。
    overage_amount: float | None


class CostGuidanceOut(Out):
    option_id: str
    currency: str
    premium_over_cheapest_compliant: float | None
    cheapest_compliant_option_id: str | None
    policy_overages: list[dict[str, Any]]
    approver_id: str | None
    tradeoffs: list[dict[str, Any]]


class OptionOut(Out):
    option_id: str
    version: int
    trip_request_version: int
    inventory_snapshot_ids: list[str]
    inventory_refs: list[str]
    legs: list[TransportOfferOut]
    fares: list[FareOut]
    outbound: TransportOfferOut
    inbound: TransportOfferOut | None
    stays: list[HotelOfferOut]
    hotel: HotelOfferOut | None
    total_cost: float
    total_duration_minutes: int
    currency: str
    feasibility: FeasibilityOut
    preference_penalty: float
    score: float
    policy_outcome: PolicyOutcome
    rule_evidence: list[RuleEvidenceOut]
    cost_guidance: CostGuidanceOut
    facts: list[str]


class ApprovalStepOut(Out):
    approver_id: str
    label: str
    status: ApprovalStatus
    decided_at: datetime | None
    reason: str | None


class ApprovalOut(Out):
    approval_id: str
    subject_hash: str
    option_id: str
    option_version: int
    trip_request_version: int
    policy_snapshot_id: str
    employee_snapshot_id: str
    violations: list[str]
    business_reason: str
    approver_id: str
    approved_price: float
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus
    decision_reason: str | None
    steps: list[ApprovalStepOut]
    current_step: int


class HandoffOut(Out):
    provider: str
    url_or_instructions: str
    expires_at: datetime


class BookingIntentOut(Out):
    intent_id: str
    idempotency_key: str
    selected_option_id: str
    selected_option_version: int
    handoff: HandoffOut
    revalidated_at: datetime
    status: str


class BookingConfirmationOut(Out):
    confirmation_id: str
    intent_id: str
    option_id: str
    option_version: int
    order_references: list[str]
    total_amount: float
    currency: str
    source: BookingConfirmationSource
    reported_by: str
    reported_at: datetime
    booked_at: datetime
    note: str | None
    planned_total: float | None
    planned_currency: str | None
    cost_variance: float | None


class ExpenseReconciliationOut(Out):
    reconciliation_id: str
    expense_id: str
    source_system: str
    expense_amount: float
    currency: str
    expensed_at: datetime
    reconciled_at: datetime
    status: ReconciliationStatus
    matched_order_references: list[str]
    note: str | None
    amount_variance: float | None


class TaskResponse(Out):
    task_id: str
    intent_entrypoint: str
    state: TaskState
    traveler_id: str
    requester_id: str
    is_delegated: bool
    trip_id: str | None
    parent_task_id: str | None
    change_event_id: str | None
    is_change_task: bool
    change_event: dict[str, Any] | None
    request_version: int | None
    booking_scope: str | None
    client_location: str | None
    commitments: list[CommitmentOut]
    transport_legs: list[TripLegOut]
    failure: str | None
    failure_details: list[Any]
    provider_retry: dict[str, Any] | None
    coverage_notices: list[Any]
    intent_fields: dict[str, Any]
    missing_required_fields: list[str]
    conflicts: list[str]
    assumptions: list[str]
    clarification_question: str | None
    clarification_questions: list[Any]
    uncertain_slots: list[Any]
    clarification_rounds: int
    manipulation_detected: bool
    tool_budget: ToolBudgetOut
    messages: list[MessageOut]
    original_instruction: str | None
    scoring: ScoringOut | None
    travel_profile: dict[str, Any] | None
    budget_snapshot: dict[str, Any] | None
    agentic_proposal: dict[str, Any] | None
    extract_failure: Any
    model_fallback: Any
    options: list[OptionOut]
    selected_option_id: str | None
    approval: ApprovalOut | None
    booking_intent: BookingIntentOut | None
    booking_confirmation: BookingConfirmationOut | None
    expense_reconciliation: ExpenseReconciliationOut | None
    summary: Literal[False]


class AuditEventOut(Out):
    event_id: str
    task_id: str
    event_type: str
    actor_type: str
    input_hash: str
    output_hash: str
    evidence_refs: list[str]
    created_at: datetime


class TaskStepsResponse(Out):
    task_id: str
    count: int
    #: 每一步的形状由 `services/task_steps.py` 决定（种类不同、字段不同），这里不复制一份。
    steps: list[dict[str, Any]]


class InventorySnapshotResponse(Out):
    snapshot_id: str
    provider: str
    source_type: SourceType
    captured_at: datetime
    valid_until: datetime
    query_hash: str
    raw_payload_hash: str
    #: 交通和酒店报价混排，各自的字段不同。
    items: list[dict[str, Any]]
    provider_warnings: list[str]
    raw_response: dict[str, Any]


# -- 政策 --------------------------------------------------------------------


class LevelRuleOut(Out):
    level: str
    allowed_flight_classes: list[str]
    allowed_train_classes: list[str]


class CityCapOut(Out):
    city: str
    nightly_cap: str


class SeasonalCapOut(Out):
    city: str
    label: str
    season_from: str
    season_to: str
    nightly_cap: str


class CostCenterBudgetOut(Out):
    cost_center: str
    amount: str
    currency: str
    period_from: str
    period_to: str


class PolicyResponse(Out):
    snapshot_id: str
    policy_version: str
    content_hash: str
    currency: str
    effective_from: str
    effective_to: str | None
    arrival_buffer_minutes: int
    viewer_level: str | None
    level_rules: list[LevelRuleOut]
    hotel_city_caps: list[CityCapOut]
    exception_allowed_rule_ids: list[str]
    min_advance_booking_days: int | None
    hotel_seasonal_caps: list[SeasonalCapOut]
    cost_center_budgets: list[CostCenterBudgetOut]


# -- 差旅 --------------------------------------------------------------------


class TripWatchLegOut(Out):
    ref_id: str
    provider: str
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime


class FlightObservationOut(Out):
    ref_id: str
    status: FlightStatusKind
    observed_at: datetime
    source: str
    verdict: ChangeImpactVerdict
    reasons: list[str]
    estimated_depart_at: datetime | None
    estimated_arrive_at: datetime | None
    opened_task_id: str | None


class TripWatchOut(Out):
    task_id: str
    legs: list[TripWatchLegOut]
    registered_at: datetime
    watch_until: datetime
    next_check_at: datetime | None
    last_checked_at: datetime | None
    check_count: int
    observations: list[FlightObservationOut]


class ChangeImpactOut(Out):
    verdict: ChangeImpactVerdict
    reasons: list[str]
    assessed_at: datetime
    delay_minutes: int | None
    new_arrive_at: datetime | None
    latest_acceptable_arrival: datetime | None
    buffer_minutes: int
    connection_ok: bool | None


class TripEventOut(Out):
    event_id: str
    event_type: TripEventType
    received_at: datetime
    reported_by: str
    ref_id: str | None
    new_depart_at: datetime | None
    new_arrive_by: datetime | None
    note: str | None
    opened_task_id: str | None
    leg_index: int | None
    impact: ChangeImpactOut | None


class TripAggregateResponse(Out):
    trip_id: str
    traveler_id: str
    requester_id: str
    status: TripStatus
    task_ids: list[str]
    created_at: datetime
    watch: TripWatchOut | None
    events: list[TripEventOut]


class FlightStatusObservationResponse(Out):
    observation: FlightObservationOut
    trip: TripAggregateResponse


class FlightStatusWebhookResponse(Out):
    results: list[FlightStatusObservationResponse]
    trip_count: int


class TripWatchRunResponse(Out):
    processed: list[str]
    source: str
    metrics: dict[str, int]


# -- 管理端 --------------------------------------------------------------------


class ExpenseImportResponse(Out):
    started_at: str
    imported: int
    skipped_duplicates: int
    by_status: dict[str, int]
    outcomes: list[dict[str, Any]]


class ExpenseRecordOut(Out):
    expense_id: str
    employee_id: str
    amount: str
    currency: str
    expensed_at: datetime
    order_references: list[str]
    source_system: str
    cost_center: str | None
    description: str | None
    status: str
    matched_task_id: str | None
    note: str | None
    imported_at: datetime


class OutboxDispatchResponse(Out):
    started_at: str
    delivered: list[str]
    failed: list[dict[str, str]]
    dead_lettered: list[str]
    channel: str
    unpublished_count: int


class OutboxEventOut(Out):
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    created_at: datetime
    published_at: datetime | None
    attempt_count: int
    last_error: str | None
    dead_lettered: bool
    payload: dict[str, Any]


class WhereaboutsOut(Out):
    trip_id: str
    task_id: str
    traveler_id: str
    requester_id: str
    status: str
    location: str
    current_leg: TripWatchLegOut | None
    next_leg: TripWatchLegOut | None
    trip_status: str
    change_pending: bool
    watch_until: datetime


class DutyOfCareResponse(Out):
    at: datetime
    travelers: list[WhereaboutsOut]


class BudgetLineOut(Out):
    cost_center: str
    currency: str
    limit: str
    period_from: date
    period_to: date
    spent: str | None
    committed: str
    remaining: str | None
    ledger_available: bool


class BudgetsResponse(Out):
    policy_snapshot_id: str
    budgets: list[BudgetLineOut]
