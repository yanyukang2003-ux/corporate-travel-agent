"""第 02 步的验收闸门：手工构造一个三段行程，端到端出方案。

**不调模型、不访问外网。** 这正是这道闸门的意义——多城能不能规划，是后端自己的事，
和模型读不读得懂原话是两件事。语义层今天还只产出 1–2 段（方案第 05 步），
所以这里直接把三段的 `journey` 写死喂进去。

**航段**：一次起讫（北京→上海）。单程 1 段、往返 2 段、多城 N 段——
行程类型是**数出来的**，不是声明的。

此前这条路走不通的原因不在请求侧：`TripRequestVersion.journey` 早就能放 6 段，
但**结果侧只有两个槽**（`TravelOptionVersion.outbound` / `.inbound`），
可行性校验也只收两段。第 02 步把这两处改成了航段列表。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from corporate_travel_agent.domain.enums import (
    BookingScope,
    TransportMode,
    TripLegRole,
)
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    LevelTravelRule,
    PolicySnapshot,
    ScopedRequirement,
    TransportOffer,
    TripLeg,
    TripRequestVersion,
)
from corporate_travel_agent.planning.feasibility import (
    FeasibilityValidator,
    planned_leg_count,
)
from corporate_travel_agent.planning.planner import ItineraryPlanner

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 5, tzinfo=UTC)

# 北京 → 上海（9/15 开会）→ 杭州（9/18 见客户）→ 北京（9/19 回）
JOURNEY = (
    TripLeg(
        role=TripLegRole.OUTBOUND,
        origin="Beijing",
        destination="Shanghai",
        depart_after=datetime(2026, 9, 15, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 15, 22, 0, tzinfo=SH),
    ),
    TripLeg(
        role=TripLegRole.OUTBOUND,
        origin="Shanghai",
        destination="Hangzhou",
        depart_after=datetime(2026, 9, 18, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 18, 13, 0, tzinfo=SH),
    ),
    TripLeg(
        role=TripLegRole.RETURN,
        origin="Hangzhou",
        destination="Beijing",
        depart_after=datetime(2026, 9, 19, 15, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 19, 23, 30, tzinfo=SH),
    ),
)


def _policy(**overrides: object) -> PolicySnapshot:
    base = PolicySnapshot(
        snapshot_id="multicity-policy",
        policy_version="multicity-v1",
        level_rules={"L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        hotel_city_caps={"Shanghai": Decimal("2000")},
        arrival_buffer_minutes=60,
        exception_allowed_rule_ids=frozenset(),
        effective_from=date(2026, 1, 1),
        currency="CNY",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _employee() -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="multicity-employee",
        employee_id="E-MC",
        level="L1",
        department="Sales",
        home_city="Beijing",
        manager_id="M-MC",
    )


def _request(**overrides: object) -> TripRequestVersion:
    payload: dict[str, object] = {
        "task_id": "multicity-task",
        "version": 1,
        "traveler_id": "E-MC",
        # 扁平字段仍然只描述得了第一段——这正是它们的极限，
        # 三段行程必须靠 journey 表达。
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": JOURNEY[0].depart_after,
        "arrive_by": JOURNEY[0].arrive_before,
        "return_after": JOURNEY[2].depart_after,
        "return_before": JOURNEY[2].arrive_before,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "booking_scope": BookingScope.ROUND_TRIP,
        "journey": JOURNEY,
    }
    payload.update(overrides)
    return TripRequestVersion.from_flat(**payload)  # type: ignore[arg-type]


def _offer(
    ref_id: str,
    leg: TripLeg,
    *,
    hour: int,
    hours: int = 2,
    minutes: int = 0,
    price: str = "600",
    mode: TransportMode = TransportMode.FLIGHT,
    seat_class: str | None = None,
    is_direct: bool = True,
) -> TransportOffer:
    day = leg.depart_after.date()
    depart = datetime(day.year, day.month, day.day, hour, 0, tzinfo=SH)
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="multicity-snap",
        provider="mock",
        mode=mode,
        origin=leg.origin,
        destination=leg.destination,
        depart_at=depart,
        arrive_at=depart.replace(hour=hour + hours, minute=minutes),
        price=Decimal(price),
        seat_class=seat_class
        or ("ECONOMY" if mode is TransportMode.FLIGHT else "SECOND_CLASS"),
        is_direct=is_direct,
        currency="CNY",
    )


def _leg_offers() -> list[list[TransportOffer]]:
    return [
        [
            _offer("BJ-SH-AM", JOURNEY[0], hour=8, price="900"),
            _offer("BJ-SH-PM", JOURNEY[0], hour=15, price="600"),
        ],
        [
            _offer(
                "SH-HZ-TRAIN",
                JOURNEY[1],
                hour=8,
                hours=1,
                price="200",
                mode=TransportMode.TRAIN,
            ),
            _offer("SH-HZ-FLIGHT", JOURNEY[1], hour=9, hours=1, price="700"),
        ],
        [
            _offer("HZ-BJ-EVE", JOURNEY[2], hour=18, price="800"),
        ],
    ]


def _plan(
    request: TripRequestVersion | None = None,
    leg_offers: list[list[TransportOffer]] | None = None,
    *,
    limit: int = 3,
    policy: PolicySnapshot | None = None,
) -> list:
    return ItineraryPlanner().plan(
        request or _request(),
        _employee(),
        policy or _policy(),
        leg_offers=leg_offers if leg_offers is not None else _leg_offers(),
        hotel_offers=[],
        limit=limit,
        now=NOW,
    )


def test_a_three_leg_trip_is_counted_as_three_legs() -> None:
    """段数是数出来的：journey 有三段，就要挑三段的报价。"""
    assert planned_leg_count(_request()) == 3


def test_a_three_leg_trip_plans_end_to_end() -> None:
    """本步的闸门：三段行程真的出得来方案，而且每条方案都带着三段。"""
    options = _plan()

    assert options, "三段行程没有出任何方案"
    for option in options:
        assert len(option.legs) == 3
        assert [leg.origin for leg in option.legs] == ["Beijing", "Shanghai", "Hangzhou"]
        assert [leg.destination for leg in option.legs] == [
            "Shanghai",
            "Hangzhou",
            "Beijing",
        ]
        # 总价与总时长都是三段加起来的，不是只算头两段。
        assert option.total_cost == sum(leg.price for leg in option.legs)
        assert option.total_duration_minutes == sum(
            int((leg.arrive_at - leg.depart_at).total_seconds() // 60)
            for leg in option.legs
        )
        assert len(option.inventory_refs) == 3


def test_the_old_two_slot_names_still_point_at_the_first_two_legs() -> None:
    """`outbound` / `inbound` 降级成了属性——前端与评测读到的东西不变。"""
    option = _plan(limit=1)[0]

    assert option.outbound is option.legs[0]
    assert option.inbound is option.legs[1]
    # 第三段没有旧名字可用，只能从 legs 读——这正是为什么要有 legs。
    assert option.legs[2].origin == "Hangzhou"


def test_the_third_leg_shows_up_in_the_explanation_facts() -> None:
    """解释事实里前两段沿用旧名字，第三段起才用 leg2、leg3。"""
    option = _plan(limit=1)[0]
    facts = dict(item.split("=", 1) for item in option.explanation_facts)

    assert facts["outbound"] == option.legs[0].ref_id
    assert facts["inbound"] == option.legs[1].ref_id
    assert facts["leg2"] == option.legs[2].ref_id


def test_the_middle_leg_is_judged_by_its_own_window_not_the_return_window() -> None:
    """第 1 段在三段行程里是中间段，不是返程。

    此前 `leg_spec(1)` 读的是扁平的 `return_after` / `return_before`——
    三段行程里那是**最后一段**的时间窗，拿来判中途那一段会把好方案判成不可行。
    """
    spec = FeasibilityValidator()
    middle = _offer("SH-HZ-OK", JOURNEY[1], hour=8, hours=1, mode=TransportMode.TRAIN)

    assert spec.leg_reasons(_request(), 1, middle, 60, now=NOW) == ()
    # 中途这一班若拿返程窗口（9/19 下午）去判，只会得出"早于允许窗口"这种错话。
    assert middle.depart_at < _request().return_after


def test_a_leg_with_no_usable_inventory_blocks_the_whole_trip() -> None:
    """任何一段没货，整趟就出不了方案——绝不许悄悄退化成两段。"""
    offers = _leg_offers()
    offers[1] = []

    assert _plan(leg_offers=offers) == []


def test_a_constraint_scoped_to_the_third_leg_only_binds_that_leg() -> None:
    """"最后一段要直飞"只管最后一段，不该放大到全程。"""
    offers = _leg_offers()
    # 第一段只剩中转的；最后一段直飞与中转各一。
    offers[0] = [_offer("BJ-SH-STOP", JOURNEY[0], hour=8, is_direct=False)]
    offers[2] = [
        _offer("HZ-BJ-STOP", JOURNEY[2], hour=17, price="500", is_direct=False),
        _offer("HZ-BJ-DIRECT", JOURNEY[2], hour=18, price="800"),
    ]
    request = _request(
        hard_constraints=("direct_only",),
        scoped_hard_constraints=(ScopedRequirement(name="direct_only", leg_index=2),),
    )

    options = _plan(request, offers)

    assert options, "只管第三段的直飞要求把整趟都筛掉了"
    for option in options:
        # 第一段照旧允许中转，第三段必须直飞。
        assert option.legs[0].ref_id == "BJ-SH-STOP"
        assert option.legs[2].ref_id == "HZ-BJ-DIRECT"


def test_arrive_before_meeting_scoped_to_a_later_leg_now_binds_there() -> None:
    """标到第 1 段的到场缓冲，现在真的作用在第 1 段上。

    此前安全缓冲只看第 0 段，标到别的段上会被无声丢掉——旅行者说了"第二段要赶客户
    会议"，系统照旧推荐一班刚好卡点到的车。
    """
    offers = _leg_offers()
    # 这班 12:30 到，卡在第 1 段的 13:00 期限内，但留不出 60 分钟缓冲。
    offers[1] = [
        _offer(
            "SH-HZ-TIGHT",
            JOURNEY[1],
            hour=11,
            hours=1,
            minutes=30,
            mode=TransportMode.TRAIN,
        )
    ]

    without_buffer = _plan(_request(), offers)
    with_buffer = _plan(
        _request(
            hard_constraints=("arrive_before_meeting",),
            scoped_hard_constraints=(
                ScopedRequirement(name="arrive_before_meeting", leg_index=1),
            ),
        ),
        offers,
    )

    assert without_buffer, "不要求缓冲时这班应当可用"
    assert with_buffer == [], "要求第 1 段留缓冲时这班赶不上，不该出现在方案里"


@pytest.mark.parametrize("leg_index", [0, 1, 2])
def test_every_leg_gets_its_own_feasibility_verdict(leg_index: int) -> None:
    """三段各判各的：哪一段不可行，报错就指着哪一段。"""
    offers = _leg_offers()
    broken = replace(offers[leg_index][0], available=False)
    offers[leg_index] = [broken]

    reasons = (
        FeasibilityValidator()
        .validate(
            _request(),
            [group[0] for group in offers],
            None,
            60,
            now=NOW,
        )
        .reasons
    )

    # "return" 这个名字只留给**两段**行程的第二段——三段行程里第 2 段不是返程，
    # 硬管它叫返程只会让报错说假话。
    labels = {0: "outbound", 1: "leg 1", 2: "leg 2"}
    assert f"{labels[leg_index]} inventory is unavailable" in reasons
