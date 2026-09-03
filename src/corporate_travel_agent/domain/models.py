"""domain.models：差旅领域实体与值对象（员工、政策、库存、方案、任务等）。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .enums import (
    ApprovalStatus,
    BookingConfirmationSource,
    BookingScope,
    ChangeImpactVerdict,
    FlightStatusKind,
    PolicyOutcome,
    PreferenceOrigin,
    RawResponseAccessPolicy,
    ReconciliationStatus,
    RevalidationStatus,
    SourceType,
    TaskState,
    ToolCallStatus,
    TransportMode,
    TripEventType,
    TripLegRole,
    TripStatus,
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
    #: 成本中心。预算规则按它找这位员工的预算，下单确认按它扣减。
    #: None 是"档案里没填"，规则不会因此凭空判——它只是不判预算这一条。
    cost_center: str | None = None
    #: 可以替这位员工订差旅的人（助理替高管订、项目经理替组员订）。
    #: 差标、审批、预算全看**旅行者**的档案；委托只决定"谁能发起"。
    delegate_ids: tuple[str, ...] = ()


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
class SeasonalHotelCap:
    """某座城市在某个日期窗口内的夜费上限（旺季比平时高，或者会展季比平时低）。

    入住日落在 ``[season_from, season_to]`` 里，这一条就**替代**基础上限——
    一晚只有一个上限说了算，不会同时判两次。
    """

    city: str
    season_from: date
    season_to: date
    nightly_cap: Decimal
    label: str

    def covers(self, day: date) -> bool:
        return self.season_from <= day <= self.season_to


@dataclass(frozen=True, slots=True)
class CostCenterBudget:
    """一个成本中心在一个预算期内的差旅预算上限。

    上限写在政策快照里（它是公司的规则）；**用掉多少不在这里**——那来自员工回填的
    下单确认，由 `services/budget_ledger.py` 在规划那一刻算出来，钉成 `BudgetSnapshot`。
    """

    cost_center: str
    amount: Decimal
    period_from: date
    period_to: date
    currency: str


@dataclass(frozen=True, slots=True)
class ApprovalTier:
    """审批链上追加的一级：什么情况下要多一个人批、由谁批。

    两种触发条件，满足其一即追加：方案总价超过 `above_amount`；或者违规/判不了的规则里
    有 `when_rules` 中的任何一条（比如预算超支永远要财务看一眼）。
    """

    label: str
    approver_id: str
    above_amount: Decimal | None = None
    when_rules: frozenset[str] = frozenset()

    def applies(self, price: Decimal, rule_ids: Iterable[str]) -> bool:
        if self.above_amount is not None and price > self.above_amount:
            return True
        return bool(self.when_rules.intersection(rule_ids))


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
    #: 至少提前几天订。None 是"这版政策没有这条规则"，不是 0。
    min_advance_booking_days: int | None = None
    #: 淡旺季夜费上限，按城市和日期窗口。为空就只有基础上限。
    hotel_seasonal_caps: tuple[SeasonalHotelCap, ...] = ()
    #: 成本中心 → 预算。为空就没有预算规则。
    cost_center_budgets: dict[str, CostCenterBudget] = field(default_factory=dict)
    #: 直属经理之后追加的审批级别，按配置顺序。为空就只有经理一级。
    approval_tiers: tuple[ApprovalTier, ...] = ()

    def seasonal_cap_for(self, city: str, day: date) -> SeasonalHotelCap | None:
        """入住日 ``day`` 在 ``city`` 适用哪条淡旺季上限；没有就是 None。

        窗口重叠时取上限**最低**的那条：两条都说自己适用，就按更严的判——
        放宽是审批人的事，不是引擎替公司做的决定。
        """
        matches = [
            item
            for item in self.hotel_seasonal_caps
            if item.city == city and item.covers(day)
        ]
        if not matches:
            return None
        return min(matches, key=lambda item: (item.nightly_cap, item.label))


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """规划那一刻，这位员工的成本中心还剩多少预算。

    和政策快照、习惯画像一样是**快照**：算出来钉进任务，此后这趟任务再规划多少次都
    用同一份。别人下一秒确认了一单、余额变了，这趟任务"当初为什么这么判"仍然答得上来。

    `spent` 来自员工回填的下单确认（`BookingConfirmation`），**是自述，不是费控回执**；
    费控对账接上之前，这个数的可信度和确认记录一样。
    """

    snapshot_id: str
    cost_center: str
    currency: str
    limit: Decimal
    spent: Decimal
    period_from: date
    period_to: date
    computed_at: datetime
    #: 用掉的数从哪来。今天只有一种：本系统里回填的下单确认。
    source: str = "booking_confirmations"

    @property
    def remaining(self) -> Decimal:
        """还能花多少；超支了就是负数，不截断——负数本身就是信息。"""
        return self.limit - self.spent


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
class ApprovalStep:
    """分级审批里的一级：谁来批、批了没有。"""

    approver_id: str
    label: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: datetime | None = None
    reason: str | None = None


@dataclass(slots=True)
class ApprovalRequest:
    """例外审批请求（绑定方案哈希与违规规则）。

    `steps` 是审批链：第一级永远是直属经理，后面按政策的 `approval_tiers` 追加
    （金额超过某档、或者触发了某条规则）。`approver_id` 始终是**当前**该批的那个人——
    投影、收件箱和前端都读它，旧任务没有 `steps` 时它就是唯一一级。
    """

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
    steps: tuple[ApprovalStep, ...] = ()
    current_step: int = 0

    @property
    def pending_step(self) -> ApprovalStep | None:
        """当前该批的那一级；没有分级信息（旧任务）时为 None，此时看 `approver_id`。"""
        if not self.steps or self.current_step >= len(self.steps):
            return None
        return self.steps[self.current_step]

    @property
    def remaining_steps(self) -> int:
        """当前这一级之后还有几级。"""
        if not self.steps:
            return 0
        return max(len(self.steps) - self.current_step - 1, 0)


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
class ExpenseReconciliation:
    """费控系统的记录和这趟任务的下单确认对上了：自述有了外部佐证。

    它是**另一条记录**，不改动 `BookingConfirmation`——确认是员工当时说的话，对账是
    费控后来说的话，两句话都要留着。金额对不上时两边都在，差额（`amount_variance`）
    由此算出来；币种不同不换算。
    """

    reconciliation_id: str
    expense_id: str
    source_system: str
    expense_amount: Decimal
    currency: str
    expensed_at: datetime
    reconciled_at: datetime
    status: ReconciliationStatus
    matched_order_references: tuple[str, ...]
    note: str | None = None

    def amount_variance(self, confirmation: BookingConfirmation) -> Decimal | None:
        """费控金额减自述金额；币种不同就是 None。"""
        if confirmation.currency != self.currency:
            return None
        return self.expense_amount - confirmation.total_amount


@dataclass(frozen=True, slots=True)
class TripWatchLeg:
    """被盯着的一段：哪张票、从哪到哪、什么时候走。变更事件按 `ref_id` 对上它。"""

    ref_id: str
    provider: str
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime


@dataclass(frozen=True, slots=True)
class FlightStatusReport:
    """航班动态源对一段交通的一次回答：状态、预计起降时刻、谁说的、什么时候说的。

    这是**输入**，不是判断。它可能来自轮询的动态源、企业 TMC 的推送、也可能是管理员
    手工报的。对已订行程有什么影响，由 `services/change_impact.assess_flight_change`
    这段确定性代码算，不由报告者说了算。
    """

    ref_id: str
    status: FlightStatusKind
    observed_at: datetime
    source: str
    #: 新的预计起降时刻。按计划 / 取消 / 查不到时为 None。
    estimated_depart_at: datetime | None = None
    estimated_arrive_at: datetime | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeImpact:
    """一条航班动态对已订行程的影响——确定性代码算出来的结论和它的依据。

    `reasons` 是给旅行者和审计看的人话；`latest_acceptable_arrival` 是这一段最晚
    几点到还来得及（到场时限减去政策的安全缓冲），`new_arrive_at` 是动态源说的
    新到达时刻。两者摆在一起，"为什么要改期"或"为什么只通知"就说得出口。
    """

    verdict: ChangeImpactVerdict
    reasons: tuple[str, ...]
    assessed_at: datetime
    delay_minutes: int | None = None
    new_arrive_at: datetime | None = None
    latest_acceptable_arrival: datetime | None = None
    buffer_minutes: int = 0
    #: 和下一段接不接得上；没有下一段时为 None。
    connection_ok: bool | None = None


@dataclass(frozen=True, slots=True)
class FlightObservation:
    """观察对象上某一段最近一次航班动态，以及系统对它的判断。

    每段只留最近一次——历史在被观察任务的审计事件里（`FLIGHT_STATUS_OBSERVED`）。
    """

    ref_id: str
    status: FlightStatusKind
    observed_at: datetime
    source: str
    verdict: ChangeImpactVerdict
    reasons: tuple[str, ...]
    estimated_depart_at: datetime | None = None
    estimated_arrive_at: datetime | None = None
    #: 这次观察开出的改期任务；没开就是 None。
    opened_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class TripWatch:
    """下单确认之后登记的观察对象：盯到最后一段出发后一天为止。

    `next_check_at` 是 watch worker 下一次该去问动态源的时刻（`services/change_impact.
    next_flight_check_at` 算的：离起飞越近查得越勤）。None 表示没什么可查了——所有段
    都落地了，或者这趟差旅登记在接动态源之前、从没排过检查。
    """

    task_id: str
    legs: tuple[TripWatchLeg, ...]
    registered_at: datetime
    watch_until: datetime
    next_check_at: datetime | None = None
    last_checked_at: datetime | None = None
    check_count: int = 0
    observations: tuple[FlightObservation, ...] = ()

    def leg(self, ref_id: str) -> TripWatchLeg | None:
        return next((item for item in self.legs if item.ref_id == ref_id), None)

    def leg_index(self, ref_id: str) -> int | None:
        return next((i for i, item in enumerate(self.legs) if item.ref_id == ref_id), None)

    def observation(self, ref_id: str) -> FlightObservation | None:
        return next((item for item in self.observations if item.ref_id == ref_id), None)


@dataclass(frozen=True, slots=True)
class TripEvent:
    """收到的一条外部变更事件，以及它开出来的改期任务。"""

    event_id: str
    event_type: TripEventType
    received_at: datetime
    reported_by: str
    #: 航变时是受影响那张票的 ref_id；会议改期时为 None。
    ref_id: str | None
    #: 事件里给的新时间（航班新起飞时刻 / 新的最晚到达时刻）；取消时为 None。
    new_depart_at: datetime | None
    new_arrive_by: datetime | None
    note: str | None
    opened_task_id: str | None
    #: 会议改期改的是第几段的到场时限（0 起）。None 按第一段——接这个字段之前的事件都是。
    leg_index: int | None = None
    #: 航变时确定性代码算出的影响；手工报的事件没有就是 None。
    impact: ChangeImpact | None = None


@dataclass(slots=True)
class Trip:
    """一趟差旅：从第一个规划任务，到下单、到航变改期，到最后一次确认。

    此前只有 `TripTask`，任务到终态就结束了。改期任务是**第二种任务**——它得挂在同一趟
    差旅下，原任务的审计一个字不动。这就是抽这个聚合的理由；在此之前抽只会是空壳。
    """

    trip_id: str
    traveler_id: str
    requester_id: str
    status: TripStatus
    task_ids: tuple[str, ...]
    created_at: datetime
    watch: TripWatch | None = None
    events: tuple[TripEvent, ...] = ()
    persistence_revision: int = 0

    @property
    def latest_task_id(self) -> str:
        return self.task_ids[-1]

    @property
    def original_task_id(self) -> str:
        return self.task_ids[0]


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
    #: 谁发起的这趟任务。None（旧任务）等于旅行者本人。和 `employee`（旅行者）分开记：
    #: 助理替高管订时，差标看高管、审批找高管的经理、审计记两个人。
    requester_id: str | None = None
    #: 费控对账结果。有它，下单确认才算"核实过"；一个任务只对一次。
    expense_reconciliation: ExpenseReconciliation | None = None
    #: 属于哪趟差旅。None 是没接差旅聚合时建的旧任务。
    trip_id: str | None = None
    #: 改期任务记它改的是哪个任务；规划任务为 None。
    parent_task_id: str | None = None
    #: 开出这个改期任务的事件。
    change_event_id: str | None = None
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

    @property
    def is_change_task(self) -> bool:
        """是不是改期任务（由外部变更事件开出来的）。"""
        return self.parent_task_id is not None

    @property
    def requested_by(self) -> str:
        """发起人；没记（旧任务）就是旅行者本人。"""
        return self.requester_id or self.employee.employee_id

    @property
    def is_delegated(self) -> bool:
        """是不是代订：发起人不是旅行者本人。"""
        return self.requested_by != self.employee.employee_id

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
