"""domain.models：差旅领域实体与值对象（员工、政策、库存、方案、任务等）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .enums import (
    ApprovalStatus,
    BookingConfirmationSource,
    BookingScope,
    PolicyOutcome,
    PreferenceOrigin,
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
class ProfilePreference:
    """一条习惯偏好，以及它凭什么成立。

    `evidence` 是**一句给员工看的人话**，不是日志。员工问"你凭什么觉得我要坐高铁"，
    答案就是这句：「最近 5 次里有 4 次选了高铁」。说不出这句话的推断不该存在——
    一个解释不了的排序，员工用两次就不信了。
    """

    name: str
    origin: PreferenceOrigin
    evidence: str


@dataclass(frozen=True, slots=True)
class EmployeeTravelProfileSnapshot:
    """员工的差旅习惯快照：他一般怎么走，以及这个"一般"是怎么来的。

    **这份东西只改排序，不改能不能走。** 它不参与可行性过滤，不参与政策判定，
    也不改变哪些方案够格进入候选池——那三件事全部由确定性代码按请求和政策决定。
    习惯是"同样合规的几条里先看哪一条"，不是"哪一条可以走"。
    这条边界有测试盯着（`tests/test_travel_profile.py`）。

    和 `PolicySnapshot` 一样是**快照**：绑进任务后不再变。员工的习惯下个月变了，
    历史任务的排序理由仍然是当时那一份，否则"为什么当初这么排"就永远答不上来。

    没有的东西也说清楚：
    - **没有常旅客号。** 它只在真正下单时有用，而这个系统不下单。现在存进来
      只是白白多一块个人敏感数据。
    - **没有偏好航司。** `TransportOffer` 里根本没有承运人字段，只有 `provider`
      和 `ref_id`。没有字段就推不出偏好——那是供应商映射要先补的东西，
      不该在这一层假装有。
    """

    snapshot_id: str
    employee_id: str
    profile_version: int = 1
    #: 习惯偏好。名字取自 `SUPPORTED_SOFT_PREFERENCES` 同一份词表，
    #: 所以它们进的是既有那套罚分逻辑，不另开一套评分。
    preferences: tuple[ProfilePreference, ...] = ()
    #: 住过并且还会再住的酒店名。空元组表示看不出来。
    preferred_hotels: tuple[str, ...] = ()
    #: 成本中心。**这一版只是带着走**，报销匹配用得上，排序里一个字都不参与。
    cost_center: str | None = None
    #: 这份画像是从几趟已完成的行程里看出来的。0 表示纯冷启动。
    derived_from_trips: int = 0

    def names_by_origin(self, origin: PreferenceOrigin) -> frozenset[str]:
        """某一档来源下的偏好名。"""
        return frozenset(
            item.name for item in self.preferences if item.origin is origin
        )


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
class TripStay:
    """一次住宿：在哪座城市、住哪几天。

    此前住宿在请求里只有 ``hotel_check_in`` / ``hotel_check_out`` 一对日期，
    城市则默认是 ``destination``——**一个目的地、一处住宿**。多城行程有 N-1 个
    过夜点（去上海开会、再去杭州见客户，两座城市各住），一对日期放不下。
    """

    city: str
    check_in: date
    check_out: date


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
    # 有序住宿列表：一站一条。为空表示按旧的扁平字段推导（兼容既有载荷与冻结评测
    # 数据）——推导只能表达"在目的地住一次"，多城的每站住宿必须显式给 ``stays``。
    stays: tuple[TripStay, ...] = ()
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

    def lodging_stays(self) -> tuple[TripStay, ...]:
        """本次行程要订的住宿序列；这趟不住店时为空。

        给了 ``stays`` 就直接用它；没给才从扁平字段推导，推出来的那一条
        **和改动之前逐字一致**：目的地那座城市，那一对日期。
        """
        if self.stays:
            return self.stays
        if self.hotel_check_in is None or self.hotel_check_out is None:
            return ()
        return (
            TripStay(
                city=self.destination,
                check_in=self.hotel_check_in,
                check_out=self.hotel_check_out,
            ),
        )

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
    # 这一段属于哪张票。``None`` 表示它自己就是一张票——今天的分段购买。
    #
    # 同一个 ``fare_ref`` 的几段是**一张整票**：不能拆开，也不能和别的票的段混搭。
    # 整票只有一个价，记在这组的**第一段**上，其余段为 0——所以
    # ``sum(leg.price)`` 仍然是整条行程的真实总价，但**单看某一段的 price 没有意义**，
    # 要看价请先按 ``fare_ref`` 分组。这不是记账技巧，是 IATA 票价构造规则的事实：
    # 多城整票不是几张单程相加，实测便宜 15%–76%
    # （`reports/evaluation-runs/multicity-pricing-*/`）。
    fare_ref: str | None = None


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
    """单条政策规则判定证据。

    ``actual`` 和 ``threshold`` 是**给人读的字符串**，什么规则都放得下：舱位是
    "BUSINESS" 对 "ECONOMY, PREMIUM_ECONOMY"，生效窗口是一串日期。它们不参与计算。

    有些规则的阈值本来就是一个数（今天只有酒店夜费上限）。这种规则额外填三个
    带类型的字段，好让"超出差标多少钱"由确定性代码算出来，而不是让前端或模型
    去 parse 上面那两个字符串——**parse 展示文本是错误的来源**：一处改了措辞，
    另一处的数字就悄悄变了。

    规则没有数值阈值时三个字段都是 ``None``，`overage_amount` 也就是 ``None``。
    这是"这条规则谈不上差额"，不是"差额为零"。
    """

    rule_id: str
    actual: str
    threshold: str
    policy_version: str
    outcome: PolicyOutcome
    message: str
    exception_allowed: bool
    #: 实测值。只有数值型规则才有。
    actual_amount: Decimal | None = None
    #: 该规则的数值上限。
    threshold_amount: Decimal | None = None
    #: 上面两个数的币种；和政策快照的币种一致。
    amount_currency: str | None = None

    @property
    def overage_amount(self) -> Decimal | None:
        """超出阈值多少。

        没有数值阈值时是 ``None``；没超时是 ``0``——**这两者不一样**，
        "谈不上差额"和"差额为零"不能混成同一个值。
        """
        if self.actual_amount is None or self.threshold_amount is None:
            return None
        return max(self.actual_amount - self.threshold_amount, Decimal("0"))


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

    @property
    def forbidden_rule_ids(self) -> tuple[str, ...]:
        """明确判为"禁止"的规则。

        **要问"这条方案能不能走"，问这个，不要问 ``outcome``。** ``_aggregate``
        把"证据不足"排在"禁止"之上（缺数据比违规更该先说），于是一条**既违规
        又缺数据**的方案聚合出来是 `INSUFFICIENT_EVIDENCE`。证据不足这一档现在
        是可选的（走人工审批），只看聚合结论的话，被禁的方案会从"判不了"这道门
        溜出去。逐条看证据就没有这个洞。
        """
        return tuple(
            item.rule_id
            for item in self.evidence
            if item.outcome is PolicyOutcome.FORBIDDEN
        )

    @property
    def unjudged_rule_ids(self) -> tuple[str, ...]:
        """系统判不了的规则——**缺的是数据，不是许可**。

        "公司不许"和"我查不到公司的规定"是两件事。前者是结论，后者是缺口，
        而缺口是可以被补上的：这串 ID 就是要补什么的清单。
        """
        return tuple(
            item.rule_id
            for item in self.evidence
            if item.outcome is PolicyOutcome.INSUFFICIENT_EVIDENCE
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
    # 这条方案要执行的交通航段，**有序**：单程 1 段、往返 2 段、多城 N 段。
    # 此前这里是 `outbound` 和 `inbound` 两个槽——请求侧早就能表达 6 段
    # （`TripRequestVersion.journey`），结果侧却只有两个位置放得下，
    # 于是"领域模型支持多城"这句话只有一半是真的。
    legs: tuple[TransportOffer, ...]
    # 这条方案要订的住宿，**按站有序**：不住 0 处、单城 1 处、多城 N-1 处。
    # 此前这里是 `hotel` 一个槽——和 `outbound`/`inbound` 一样的毛病，只是发生在
    # 住宿上：一个位置只放得下一座城市的酒店。
    stays: tuple[HotelOffer, ...]
    total_cost: Decimal
    total_duration_minutes: int
    feasibility: FeasibilityResult
    policy_decision: PolicyDecision
    preference_penalty: Decimal
    score: Decimal
    explanation_facts: tuple[str, ...]
    currency: str = "USD"

    @property
    def outbound(self) -> TransportOffer:
        """第一段。保留这个名字，是因为它在前端、评测与 API 响应里已经叫开了。"""
        return self.legs[0]

    @property
    def inbound(self) -> TransportOffer | None:
        """第二段；只有一段时为 None。三段以上请直接读 ``legs``。"""
        return self.legs[1] if len(self.legs) > 1 else None

    @property
    def fares(self) -> tuple[tuple[str | None, Decimal], ...]:
        """这条方案要买几张票，各多少钱。

        分段购买时一段一张票（``fare_ref`` 为 None，各按各的价）；
        多城整票时几段共用一个 ``fare_ref``，只出现一次、一个价。
        **展示价格请读这里，不要逐段读 `price`**——整票的价只记在第一段上。
        """
        totals: dict[str | None, Decimal] = {}
        order: list[str | None] = []
        for index, leg in enumerate(self.legs):
            key = leg.fare_ref if leg.fare_ref else f"__leg{index}"
            if key not in totals:
                totals[key] = Decimal("0")
                order.append(key)
            totals[key] += leg.price
        return tuple(
            (None if str(key).startswith("__leg") else key, totals[key]) for key in order
        )

    @property
    def hotel(self) -> HotelOffer | None:
        """第一处住宿；不住店时为 None。两处以上请直接读 ``stays``。

        保留这个名字，是因为它在前端、评测与 API 响应里已经叫开了。
        """
        return self.stays[0] if self.stays else None

    @property
    def inventory_refs(self) -> tuple[str, ...]:
        """方案引用的库存 ref_id 列表。"""
        refs = [leg.ref_id for leg in self.legs]
        refs.extend(stay.ref_id for stay in self.stays)
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
class BookingConfirmation:
    """员工回填的下单确认：订单号、实付金额，以及这话是谁说的。

    这是交接之后系统能拿到的**第一条**"真的订了"的证据。在它之前，`HANDOFF_COMPLETED`
    记录的只是员工点了一下"我去订了"；有了它，业务指标层的交接完成率才有一个能对照的
    分子，提前预订天数才有一个真实的下单时刻，"计划花多少 / 实际花多少"才算得出来。

    **它是自述，不是回执。** `source` 今天只有 `SELF_REPORTED` 一档；订单号系统核不了，
    金额系统核不了。读它的人（指标、报表）必须把这一点带着走，不能把它当供应商回执用。
    费控对账接上之后会有第二档来源，那时才能谈"核实过的"。

    不可变、一个任务只有一条：填错了不改，开新任务。改一条已经进了指标的记录，
    等于让历史曲线悄悄变形。
    """

    confirmation_id: str
    #: 它确认的是哪一次交接意图——和 `BookingIntent.intent_id` 对得上。
    intent_id: str
    option_id: str
    option_version: int
    #: 订单号 / PNR（航空订座记录编号）。一趟行程可能几张票几个号，所以是列表；至少一个。
    order_references: tuple[str, ...]
    #: 员工实际付了多少。**只是一个总数**——分到每张票每晚房要等费控对账。
    total_amount: Decimal
    currency: str
    source: BookingConfirmationSource
    #: 谁填的：员工本人的 ID，或者替他操作的管理员 ID。
    reported_by: str
    #: 系统收到这条回填的时刻（编排器时钟）。
    reported_at: datetime
    #: 员工说的下单时刻。没说就等于 `reported_at`。提前预订天数用它算。
    booked_at: datetime
    note: str | None = None


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


@dataclass(frozen=True, slots=True)
class SearchProvenance:
    """一次库存搜索的**出处**：为什么搜了这一次，以及它产出了哪个快照。

    这条记录补的是溯源链上唯一断掉的一环。此前从方案倒推，能一路走到
    "这张票来自快照 X、原始响应的 sha256 是 Y"——**但走不回"为什么搜的是
    9 月 15 日"**。工具循环里那道日期出处关卡
    （`ToolExecutor._require_quoted_evidence`）本来就逼模型逐字抄一句用户原话
    来证明这一天不是它自己想的，可那句话验完就被丢掉了。

    现在它被留下来：`date_evidence` 是用户原话，`assumption` 是系统自己推的那
    一步（比如"到达时限往前 18 小时"）。两者分开存，因为它们的性质不同——
    一个是人说的，一个是机器算的，**混在一起就分不清哪句话是谁的责任**。
    """

    #: `transport` 或 `hotel`。
    kind: str
    #: 这次搜索真正用的参数，有序。存成字符串对，是为了两类搜索共用一种形状，
    #: 也为了这条记录在 JSON 里读起来就是人能看懂的样子。
    parameters: tuple[tuple[str, str], ...]
    snapshot_id: str
    query_hash: str
    captured_at: datetime
    valid_until: datetime
    #: 支撑这次搜索日期的**用户原话**，逐字。结构化入口没有对话，因此为 None。
    date_evidence: str | None = None
    #: 系统自己补的那一步推导，写给用户看过的那句话。
    assumption: str | None = None


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
    #: 员工回填的下单确认。交接之后唯一的新事实；一个任务只有一条，写了不改。
    booking_confirmation: BookingConfirmation | None = None
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
    #: 每次库存搜索的出处，按发生顺序。旧任务反序列化时为空列表。
    searches: list[SearchProvenance] = field(default_factory=list)
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

    def confirmed_option(self) -> TravelOptionVersion | None:
        """下单确认对应的那条方案；没有确认记录时为 None。

        按确认记录上的 `option_id` 找，不按 `selected_option_id`——两者按构造是同一个，
        但确认记录是不可变的事实，选中 ID 是可变的状态，读事实要跟着事实走。
        """
        confirmation = self.booking_confirmation
        if confirmation is None:
            return None
        return next(
            (item for item in self.options if item.option_id == confirmation.option_id),
            None,
        )

    def booking_cost_variance(self) -> Decimal | None:
        """实付比方案价多付（正）或少付（负）了多少；算不出来就是 None。

        算不出来的两种情况：没有确认记录或找不到对应方案；币种对不上。
        **币种不一致时不换算**——省的是 800 什么？说不出来就不凑。
        和 `planning/cost_guidance.py` 是同一条规矩。
        """
        confirmation = self.booking_confirmation
        option = self.confirmed_option()
        if confirmation is None or option is None:
            return None
        if confirmation.currency != option.currency:
            return None
        return confirmation.total_amount - option.total_cost
