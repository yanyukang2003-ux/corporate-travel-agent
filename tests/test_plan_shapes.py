"""第 05 步：规划器产出的是几个真正不同的**走法**，不是同一个走法的几个价格。

**走法**：这趟差旅怎么走——每段坐飞机还是高铁、住不住。14 点那班和 16 点那班
不是两种走法，是同一种走法的两个价格。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from corporate_travel_agent.domain.enums import (
    BookingScope,
    PolicyOutcome,
    TransportMode,
)
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    LevelTravelRule,
    PolicySnapshot,
    TransportOffer,
    TripRequestVersion,
)
from corporate_travel_agent.planning.planner import ItineraryPlanner, PlanShape

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _policy(**overrides: object) -> PolicySnapshot:
    base = PolicySnapshot(
        snapshot_id="shape-policy-v1",
        policy_version="shape-v1",
        level_rules={"L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        hotel_city_caps={"Shanghai": Decimal("2000")},
        arrival_buffer_minutes=30,
        exception_allowed_rule_ids=frozenset(),
        effective_from=date(2026, 1, 1),
        currency="CNY",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _employee() -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="shape-employee-v1",
        employee_id="E-SHAPE",
        level="L1",
        department="Product",
        home_city="Beijing",
        manager_id="M-SHAPE",
    )


def _request(**overrides: object) -> TripRequestVersion:
    payload: dict[str, object] = {
        "task_id": "shape-task",
        "version": 1,
        "traveler_id": "E-SHAPE",
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": datetime(2026, 8, 20, 6, 0, tzinfo=SH),
        "arrive_by": datetime(2026, 8, 20, 23, 0, tzinfo=SH),
        "return_after": None,
        "return_before": None,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "booking_scope": BookingScope.OUTBOUND_ONLY,
    }
    payload.update(overrides)
    return TripRequestVersion.from_flat(**payload)  # type: ignore[arg-type]


def _offer(
    ref_id: str,
    *,
    mode: TransportMode = TransportMode.FLIGHT,
    hour: int = 9,
    price: str = "500",
    duration_hours: int = 2,
    seat_class: str | None = None,
) -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="snap",
        provider="mock",
        mode=mode,
        origin="Beijing",
        destination="Shanghai",
        depart_at=datetime(2026, 8, 20, hour, 0, tzinfo=SH),
        arrive_at=datetime(2026, 8, 20, hour + duration_hours, 0, tzinfo=SH),
        price=Decimal(price),
        seat_class=seat_class
        or ("ECONOMY" if mode is TransportMode.FLIGHT else "SECOND_CLASS"),
        currency="CNY",
    )


def _plan(request: TripRequestVersion, offers: list[TransportOffer], *, limit: int = 3,
          policy: PolicySnapshot | None = None) -> list:
    return ItineraryPlanner().plan(
        request,
        _employee(),
        policy or _policy(),
        leg_offers=[offers, []],
        hotel_offers=[],
        limit=limit,
        now=NOW,
    )


def _fact(option, prefix: str) -> str:
    return next(
        item.split("=", 1)[1]
        for item in option.explanation_facts
        if item.startswith(f"{prefix}=")
    )


def test_the_list_shows_different_ways_to_travel_not_one_way_at_three_prices() -> None:
    """此前三个推荐常常是同一个走法的三个价格：看着有三个选择，其实只有一个。"""
    flights = [
        _offer("FLIGHT-A", price="500", hour=9),
        _offer("FLIGHT-B", price="520", hour=11),
        _offer("FLIGHT-C", price="540", hour=13),
    ]
    train = _offer("TRAIN-A", mode=TransportMode.TRAIN, price="560", hour=8,
                   duration_hours=5)

    options = _plan(_request(), [*flights, train])
    shapes = {_fact(option, "plan_shape") for option in options}
    assert shapes == {"FLIGHT", "TRAIN"}, shapes


def test_each_shown_option_says_which_kind_it_is_best_of() -> None:
    """差旅没有唯一的最好——摆出来的每一条都要说清它凭什么在这儿。"""
    cheap_slow = _offer("CHEAP-SLOW", price="300", hour=8, duration_hours=9)
    dear_fast = _offer("DEAR-FAST", price="1500", hour=8, duration_hours=1)

    options = _plan(_request(), [cheap_slow, dear_fast], limit=2)
    categories = {option.outbound.ref_id: _fact(option, "category") for option in options}
    assert "cheapest" in categories["CHEAP-SLOW"]
    assert "fastest" in categories["DEAR-FAST"]


def test_the_cheapest_and_the_fastest_both_get_a_seat() -> None:
    """便宜的起得早、快的贵——两个都要摆出来，取舍交回给人。"""
    offers = [
        _offer("CHEAPEST", price="200", hour=8, duration_hours=10),
        _offer("MIDDLE", price="600", hour=9, duration_hours=5),
        _offer("FASTEST", price="1400", hour=10, duration_hours=1),
    ]
    shown = {item.outbound.ref_id for item in _plan(_request(), offers, limit=2)}
    assert {"CHEAPEST", "FASTEST"} <= shown


def test_policy_still_outranks_category(
) -> None:
    """分类是档内的事。一个需审批但更便宜的方案仍然排在合规方案之后。"""
    compliant = _offer("COMPLIANT", price="5000", hour=9)
    needs_approval = _offer("CHEAP-BUSINESS", price="200", hour=10, seat_class="BUSINESS")
    policy = _policy(
        exception_allowed_rule_ids=frozenset({"transport.flight.seat_class"})
    )

    options = _plan(_request(), [compliant, needs_approval], limit=3, policy=policy)
    assert [item.outbound.ref_id for item in options][:2] == [
        "COMPLIANT",
        "CHEAP-BUSINESS",
    ]
    assert options[0].policy_decision.outcome is PolicyOutcome.COMPLIANT
    assert options[1].score < options[0].score


def test_a_shape_the_inventory_cannot_supply_is_never_enumerated() -> None:
    """只枚举库存里真有的交通方式——涵盖不了的走法不靠编造来凑。"""
    options = _plan(_request(), [_offer("FLIGHT-A"), _offer("FLIGHT-B", hour=12)])
    assert {_fact(item, "plan_shape") for item in options} == {"FLIGHT"}


def test_plan_shape_label_reads_like_a_sentence() -> None:
    shape = PlanShape(
        modes=(TransportMode.FLIGHT, TransportMode.TRAIN), with_lodging=True
    )
    assert shape.label() == "FLIGHT+TRAIN+hotel"
