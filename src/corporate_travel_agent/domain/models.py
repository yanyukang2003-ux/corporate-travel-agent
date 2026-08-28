"""domain.models：差旅领域实体与值对象（员工、政策、库存、方案、任务等）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .enums import (
    ApprovalStatus,
    BookingScope,
    PolicyOutcome,
    RawResponseAccessPolicy,
    RevalidationStatus,
    SourceType,
    TaskState,
    ToolCallStatus,
    TransportMode,
    TripLegRole,
)

# Provider 无法证明客户通勤时间时的哨兵值（分钟）。
COMMUTE_UNKNOWN_MINUTES = 1_440


@dataclass(frozen=True, slots=True)
class EmployeeProfileSnapshot:
    """员工档案快照：职级、部门、常驻城市等，供政策评估使用。"""

    snapshot_id: str
    employee_id: str
    level: str
    department: str
    home_city: str
    manager_id: str
    profile_version: int = 1


@dataclass(frozen=True, slots=True)
class LevelTravelRule:
    """某一职级允许的舱位/席别规则。"""

    allowed_flight_classes: tuple[str, ...]
    allowed_train_classes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """差旅政策版本快照（职级规则、酒店上限、到达缓冲等）。"""

    snapshot_id: str
    policy_version: str
    level_rules: dict[str, LevelTravelRule]
    hotel_city_caps: dict[str, Decimal]
    arrival_buffer_minutes: int
    exception_allowed_rule_ids: frozenset[str]
    effective_from: date
    effective_to: date | None = None
    content_hash: str = ""
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class TripLeg:
    """一次真实执行的交通航段；起讫城市和时间均按该航段方向表达。"""

    role: TripLegRole
    origin: str
    destination: str
    depart_after: datetime
    arrive_before: datetime


@dataclass(frozen=True, slots=True)
class ScopedRequirement:
    """一条要求或偏好，外加它管到哪一段。

    ``leg_index`` 为 None 表示整趟行程；给了数字就只管那一段（0 是第一段）。
    在此之前要求只是一串名字，"去程直飞就行、返程无所谓"这句话没有地方安放：
    宿主要么把直飞放大到全程（多筛掉了用户接受的方案），要么整条丢掉。
    """

    name: str
    leg_index: int | None = None

    @property
    def whole_journey(self) -> bool:
        """这条要求是否管整趟行程。"""
        return self.leg_index is None


@dataclass(frozen=True, slots=True)
class Commitment:
    """旅行者必须到场的一件事：某地、某时之前、为了什么。

    这是行程的**原因**，而不是行程本身。今天它被拆散在三处：会面地点被整个丢掉、
    “会前必须到”是硬约束元组里的一个字符串、到达时限混在 `arrive_by` 里。
    拆散之后，政策永远只能查单价，没法判断“这趟差旅本身合不合理”。
    """

    place: str
    not_later_than: datetime
    purpose: str | None = None
    # 对应旧的 arrive_before_meeting：到场时间要额外留出政策规定的安全缓冲。
    safety_buffer_required: bool = False


@dataclass(frozen=True, slots=True)
class TripRequestVersion:
    """一次行程请求的不可变版本（预订范围、航段、住宿与偏好）。"""

    task_id: str
    version: int
    traveler_id: str
    origin: str
    destination: str
    departure_after: datetime
    arrive_by: datetime
    return_after: datetime | None
    return_before: datetime | None
    hotel_check_in: date | None
    hotel_check_out: date | None
    hard_constraints: tuple[str, ...] = ()
    soft_preferences: tuple[str, ...] = ()
    # 同样这些要求，但**带上它们管到哪一段**。为空表示按扁平字段理解成"管全程"
    # （兼容既有载荷与冻结评测数据）；给了就以它为准，扁平字段退化成同一批名字的去重视图。
    scoped_hard_constraints: tuple[ScopedRequirement, ...] = ()
    scoped_soft_preferences: tuple[ScopedRequirement, ...] = ()
    # None 仅用于兼容旧持久化载荷；新请求必须显式写入。
    booking_scope: BookingScope | None = None
    # 旅行者要到场的事。空元组表示这趟行程没有记录到任何到场要求。
    # 有序航段列表：单程 1 段、往返 2 段、多城 N 段——**行程类型是数出来的，不是声明的**。
    # 为空表示按旧的扁平字段推导（兼容既有载荷与冻结评测数据）。给了就以它为准。
    journey: tuple[TripLeg, ...] = ()
    commitments: tuple[Commitment, ...] = ()
    # 会面地点。此前语义层抽出来后在编译成请求时被丢掉，规划器与政策引擎从未见过它。
    client_location: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def resolved_booking_scope(self) -> BookingScope:
        """返回有效预订范围；旧载荷按是否存在返程窗口推导。"""
        if self.booking_scope is not None:
            return self.booking_scope
        if self.return_after is not None or self.return_before is not None:
            return BookingScope.ROUND_TRIP
        return BookingScope.OUTBOUND_ONLY

    def transport_legs(self) -> tuple[TripLeg, ...]:
        """本次行程要执行的航段序列。

        给了 ``journey`` 就直接用它；没给才从扁平字段推导——推导只能表达
        单程和**原路**往返，所以“去上海、从杭州回”这类行程必须显式给 ``journey``。
        """
        if self.journey:
            return self.journey
        scope = self.resolved_booking_scope
        primary_role = (
            TripLegRole.RETURN if scope is BookingScope.RETURN_ONLY else TripLegRole.OUTBOUND
        )
        legs = [
            TripLeg(
                role=primary_role,
                origin=self.origin,
                destination=self.destination,
                depart_after=self.departure_after,
                arrive_before=self.arrive_by,
            )
        ]
        if (
            scope is BookingScope.ROUND_TRIP
            and self.return_after is not None
            and self.return_before is not None
        ):
            legs.append(
                TripLeg(
                    role=TripLegRole.RETURN,
                    origin=self.destination,
                    destination=self.origin,
                    depart_after=self.return_after,
                    arrive_before=self.return_before,
                )
            )
        return tuple(legs)

    def scoped_constraints(self) -> tuple[ScopedRequirement, ...]:
        """硬要求的完整视图；没写作用域的旧请求一律按"管全程"理解。"""
        if self.scoped_hard_constraints:
            return self.scoped_hard_constraints
        return tuple(ScopedRequirement(name=name) for name in self.hard_constraints)

    def scoped_preferences(self) -> tuple[ScopedRequirement, ...]:
        """软偏好的完整视图；没写作用域的旧请求一律按"管全程"理解。"""
        if self.scoped_soft_preferences:
            return self.scoped_soft_preferences
        return tuple(ScopedRequirement(name=name) for name in self.soft_preferences)

    def constraints_for_leg(self, leg_index: int) -> frozenset[str]:
        """管到第 ``leg_index`` 段的硬要求（含管全程的那些）。"""
        return _names_for_leg(self.scoped_constraints(), leg_index)

    def preferences_for_leg(self, leg_index: int) -> frozenset[str]:
        """管到第 ``leg_index`` 段的软偏好（含管全程的那些）。"""
        return _names_for_leg(self.scoped_preferences(), leg_index)

    def journey_wide_preferences(self) -> frozenset[str]:
        """只谈整趟行程的偏好——整单加权、结果集要不要放两种交通方式这类。"""
        return frozenset(
            item.name for item in self.scoped_preferences() if item.whole_journey
        )


def _names_for_leg(
    requirements: tuple[ScopedRequirement, ...], leg_index: int
) -> frozenset[str]:
    return frozenset(
        item.name
        for item in requirements
        if item.whole_journey or item.leg_index == leg_index
    )


@dataclass(frozen=True, slots=True)
class TransportOffer:
    """单条交通库存报价（航班或火车）。"""

    ref_id: str
    snapshot_id: str
    provider: str
    mode: TransportMode
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    price: Decimal
    seat_class: str
    available: bool = True
    is_direct: bool = True
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class HotelOffer:
    """单条酒店库存报价；含夜间价与到客户通勤分钟。"""

    ref_id: str
    snapshot_id: str
    provider: str
    name: str
    city: str
    check_in: date
    check_out: date
    nightly_price: Decimal
    commute_minutes: int
    available: bool = True
    currency: str = "USD"

    @property
    def nights(self) -> int:
        """入住晚数（退房日−入住日）。"""
        return max((self.check_out - self.check_in).days, 0)

    @property
    def total_price(self) -> Decimal:
        """酒店总价 = 夜间价 × 晚数。"""
        return self.nightly_price * self.nights

    @property
    def commute_known(self) -> bool:
        """通勤时间是否为真实证据（非哨兵值）。"""
        return self.commute_minutes != COMMUTE_UNKNOWN_MINUTES


@dataclass(frozen=True, slots=True)
class RawResponseReference:
    """对象存储中原始 Provider 响应的引用与访问策略。"""

    object_key: str
    sha256: str
    size_bytes: int
    content_type: str
    stored_at: datetime
    retention_until: datetime
    access_policy: RawResponseAccessPolicy


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """一次搜索得到的库存快照（含有效期与原始响应哈希）。"""

    snapshot_id: str
    provider: str
    source_type: SourceType
    captured_at: datetime
    valid_until: datetime
    query_hash: str
    raw_payload_hash: str
    items: tuple[TransportOffer | HotelOffer, ...]
    provider_warnings: tuple[str, ...] = ()
    raw_response: RawResponseReference | None = None


@dataclass(frozen=True, slots=True)
class RuleEvidence:
    """单条政策规则判定证据。"""

    rule_id: str
    actual: str
    threshold: str
    policy_version: str
    outcome: PolicyOutcome
    message: str
    exception_allowed: bool


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """对整份行程方案的政策汇总判定。"""

    outcome: PolicyOutcome
    evidence: tuple[RuleEvidence, ...]

    @property
    def violation_ids(self) -> tuple[str, ...]:
        """需审批或禁止的规则 ID 列表。"""
        return tuple(
            item.rule_id
            for item in self.evidence
            if item.outcome in {PolicyOutcome.REQUIRES_APPROVAL, PolicyOutcome.FORBIDDEN}
        )


@dataclass(frozen=True, slots=True)
class FeasibilityResult:
    """行程可行性结果及不可行原因。"""

    feasible: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TravelOptionVersion:
    """规划器产出的一版可选行程（交通+可选酒店+政策与评分）。"""

    option_id: str
    version: int
    trip_request_version: int
    inventory_snapshot_ids: tuple[str, ...]
    outbound: TransportOffer
    inbound: TransportOffer | None
    hotel: HotelOffer | None
    total_cost: Decimal
    total_duration_minutes: int
    feasibility: FeasibilityResult
    policy_decision: PolicyDecision
    preference_penalty: Decimal
    score: Decimal
    explanation_facts: tuple[str, ...]
    currency: str = "USD"

    @property
    def inventory_refs(self) -> tuple[str, ...]:
        """方案引用的库存 ref_id 列表。"""
        refs = [self.outbound.ref_id]
        if self.inbound:
            refs.append(self.inbound.ref_id)
        if self.hotel:
            refs.append(self.hotel.ref_id)
        return tuple(refs)


@dataclass(slots=True)
class ApprovalRequest:
    """例外审批请求（绑定方案哈希与违规规则）。"""

    approval_id: str
    subject_hash: str
    option_id: str
    option_version: int
    trip_request_version: int
    policy_snapshot_id: str
    employee_snapshot_id: str
    violations: tuple[str, ...]
    business_reason: str
    approver_id: str
    approved_price: Decimal
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RevalidationResult:
    """选中方案再校验结果（价格/可用性）。"""

    status: RevalidationStatus
    checked_at: datetime
    current_prices: dict[str, Decimal]
    unavailable_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderHandoff:
    """交给外部 Provider 下单的跳转信息（V1 不代付）。"""

    provider: str
    url_or_instructions: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BookingIntent:
    """Booking Intent：再校验通过后的下单意图与幂等键。"""

    intent_id: str
    idempotency_key: str
    selected_option_id: str
    selected_option_version: int
    handoff: ProviderHandoff
    revalidated_at: datetime
    status: str = "READY"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """任务审计事件（输入/输出哈希与证据引用）。"""

    event_id: str
    task_id: str
    event_type: str
    actor_type: str
    input_hash: str
    output_hash: str
    evidence_refs: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """任务会话中的一条用户或系统消息。"""

    role: str
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class ToolCallRecord:
    """一次工具调用的审计记录（含恢复分类字段）。"""

    sequence: int
    tool_name: str
    tool_kind: str
    status: ToolCallStatus
    started_at: datetime
    completed_at: datetime | None = None
    error_type: str | None = None
    error_code: str | None = None
    error_layer: str | None = None
    retryable: bool | None = None
    retry_of: int | None = None
    reason_code: str | None = None
    side_effect_class: str | None = None
    recovery_action: str | None = None
    counts_toward_budget: bool = True


@dataclass(slots=True)
class TripTask:
    """差旅任务聚合根：状态、请求、方案、审批与工具预算。"""

    task_id: str
    state: TaskState
    request: TripRequestVersion | None
    employee: EmployeeProfileSnapshot
    policy_snapshot_id: str
    options: list[TravelOptionVersion] = field(default_factory=list)
    selected_option_id: str | None = None
    approval: ApprovalRequest | None = None
    booking_intent: BookingIntent | None = None
    failure: str | None = None
    intent_fields: dict[str, Any] = field(default_factory=dict)
    messages: list[ConversationMessage] = field(default_factory=list)
    missing_required_fields: tuple[str, ...] = ()
    intent_conflicts: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    clarification_question: str | None = None
    clarification_rounds: int = 0
    tool_call_limit: int = 12
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    persistence_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_calls_used(self) -> int:
        """已计入工具预算的调用次数。"""
        return sum(1 for item in self.tool_calls if item.counts_toward_budget)

    @property
    def tool_calls_remaining(self) -> int:
        """剩余可用工具调用次数。"""
        return max(self.tool_call_limit - self.tool_calls_used, 0)

    def selected_option(self) -> TravelOptionVersion | None:
        """当前选中的 TravelOptionVersion，未选则 None。"""
        return next(
            (item for item in self.options if item.option_id == self.selected_option_id),
            None,
        )
