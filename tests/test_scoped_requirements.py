"""第 04 步：要求与偏好带上作用域后，该管的段才管，该有的评分维度不许缺。

这里验的是三件此前确实做错的事：
1. 往返里的偏好只评了去程；
2. `lowest_cost` / `shortest_duration` / `compare_train_and_flight` 有名字没实现；
3. `direct_only` 是全局的，说不出"去程直飞就行、返程无所谓"。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from corporate_travel_agent.domain.constraints import SUPPORTED_SOFT_PREFERENCES
from corporate_travel_agent.domain.enums import BookingScope, TransportMode
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    ScopedRequirement,
    TransportOffer,
    TripRequestVersion,
)
from corporate_travel_agent.domain.validation import validate_trip_request_values
from corporate_travel_agent.planning.planner import ItineraryPlanner
from corporate_travel_agent.planning.preferences import (
    PREFERENCE_EFFECTS,
    preference_penalty,
)

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _policy() -> PolicySnapshot:
    return PolicySnapshot(
        snapshot_id="scope-policy-v1",
        policy_version="scope-v1",
        level_rules={
            "L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",)),
        },
        hotel_city_caps={"Shanghai": Decimal("2000")},
        arrival_buffer_minutes=30,
        exception_allowed_rule_ids=frozenset(),
        effective_from=date(2026, 1, 1),
        currency="CNY",
    )


def _employee() -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="scope-employee-v1",
        employee_id="E-SCOPE",
        level="L1",
        department="Product",
        home_city="Beijing",
        manager_id="M-SCOPE",
    )


def _round_trip(**overrides: object) -> TripRequestVersion:
    payload: dict[str, object] = {
        "task_id": "scope-task",
        "version": 1,
        "traveler_id": "E-SCOPE",
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        "arrive_by": datetime(2026, 8, 20, 18, 0, tzinfo=SH),
        "return_after": datetime(2026, 8, 21, 13, 0, tzinfo=SH),
        "return_before": datetime(2026, 8, 21, 23, 0, tzinfo=SH),
        "hotel_check_in": None,
        "hotel_check_out": None,
        "booking_scope": BookingScope.ROUND_TRIP,
    }
    payload.update(overrides)
    return TripRequestVersion(**payload)  # type: ignore[arg-type]


def _out(
    ref_id: str,
    *,
    mode: TransportMode = TransportMode.FLIGHT,
    hour: int = 14,
    price: str = "500",
    is_direct: bool = True,
    duration_hours: int = 2,
) -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="snap-out",
        provider="mock",
        mode=mode,
        origin="Beijing",
        destination="Shanghai",
        depart_at=datetime(2026, 8, 20, hour, 0, tzinfo=SH),
        arrive_at=datetime(2026, 8, 20, hour + duration_hours, 0, tzinfo=SH),
        price=Decimal(price),
        seat_class="ECONOMY" if mode is TransportMode.FLIGHT else "SECOND_CLASS",
        is_direct=is_direct,
        currency="CNY",
    )


def _back(
    ref_id: str,
    *,
    mode: TransportMode = TransportMode.FLIGHT,
    hour: int = 15,
    price: str = "500",
    is_direct: bool = True,
    duration_hours: int = 2,
) -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="snap-back",
        provider="mock",
        mode=mode,
        origin="Shanghai",
        destination="Beijing",
        depart_at=datetime(2026, 8, 21, hour, 0, tzinfo=SH),
        arrive_at=datetime(2026, 8, 21, hour + duration_hours, 0, tzinfo=SH),
        price=Decimal(price),
        seat_class="ECONOMY" if mode is TransportMode.FLIGHT else "SECOND_CLASS",
        is_direct=is_direct,
        currency="CNY",
    )


def _plan(
    request: TripRequestVersion,
    outbound: list[TransportOffer],
    inbound: list[TransportOffer],
    hotels: list[HotelOffer] | None = None,
    limit: int = 3,
) -> list:
    return ItineraryPlanner().plan(
        request,
        _employee(),
        _policy(),
        leg_offers=[outbound, inbound],
        hotel_offers=hotels or [],
        limit=limit,
        now=NOW,
    )


def test_every_supported_preference_has_a_scoring_dimension() -> None:
    """词表是承诺：有名字就必须有实现，否则声明与不声明毫无区别。"""
    assert set(PREFERENCE_EFFECTS) == set(SUPPORTED_SOFT_PREFERENCES)


def test_a_return_leg_is_scored_against_the_preference_too() -> None:
    """此前罚分函数压根没收返程，往返里说"优先高铁"只有去程被评。"""
    request = _round_trip(soft_preferences=("prefer_train",))
    train_out = _out("OUT-TRAIN", mode=TransportMode.TRAIN)
    flight_back = _back("BACK-FLIGHT", mode=TransportMode.FLIGHT)
    train_back = _back("BACK-TRAIN", mode=TransportMode.TRAIN)

    mixed = preference_penalty(request, [train_out, flight_back], None)
    all_train = preference_penalty(request, [train_out, train_back], None)
    assert mixed > all_train
    assert all_train == Decimal("0")


def test_direct_only_on_the_outbound_leaves_a_connecting_return_bookable() -> None:
    """「去程直飞就行、返程无所谓」——返程转机不该被去程的要求筛掉。"""
    request = _round_trip(
        hard_constraints=("direct_only",),
        scoped_hard_constraints=(ScopedRequirement("direct_only", 0),),
    )
    options = _plan(
        request,
        [_out("OUT-DIRECT", is_direct=True)],
        [_back("BACK-CONNECTING", is_direct=False)],
    )
    assert [item.inbound.ref_id for item in options if item.inbound] == ["BACK-CONNECTING"]


def test_direct_only_without_a_scope_still_governs_every_leg() -> None:
    """没写作用域的旧请求行为不变：一句话管全程。"""
    request = _round_trip(hard_constraints=("direct_only",))
    options = _plan(
        request,
        [_out("OUT-DIRECT", is_direct=True)],
        [_back("BACK-CONNECTING", is_direct=False)],
    )
    assert options == []


def test_lowest_cost_and_shortest_duration_change_which_option_wins() -> None:
    """两个偏好此前在排序代码里根本没出现，声明与不声明结果完全一样。"""
    cheap_slow = _out("OUT-CHEAP-SLOW", price="500", duration_hours=9, hour=8)
    dear_fast = _out("OUT-DEAR-FAST", price="900", duration_hours=1, hour=8)
    inbound = [_back("BACK-1")]

    cheapest = _plan(
        _round_trip(soft_preferences=("lowest_cost",)),
        [cheap_slow, dear_fast],
        inbound,
        limit=1,
    )
    fastest = _plan(
        _round_trip(soft_preferences=("shortest_duration",)),
        [cheap_slow, dear_fast],
        inbound,
        limit=1,
    )
    assert cheapest[0].outbound.ref_id == "OUT-CHEAP-SLOW"
    assert fastest[0].outbound.ref_id == "OUT-DEAR-FAST"


def test_compare_train_and_flight_puts_both_modes_on_the_list() -> None:
    """"我想比比高铁和飞机"要的是名单里两种都有，不是给某一种扣分。"""
    cheap_flights = [
        _out(f"OUT-FLIGHT-{index}", price=str(100 + index), hour=9 + index)
        for index in range(3)
    ]
    train = _out("OUT-TRAIN", mode=TransportMode.TRAIN, price="900", hour=9)
    options = _plan(
        _round_trip(soft_preferences=("compare_train_and_flight",)),
        [*cheap_flights, train],
        [_back("BACK-1")],
    )
    modes = {item.outbound.mode for item in options}
    assert modes == {TransportMode.FLIGHT, TransportMode.TRAIN}


def test_mode_comparison_does_not_invent_a_mode_the_inventory_lacks() -> None:
    """库存里只有飞机时，涵盖不了的东西不靠编造来凑。"""
    options = _plan(
        _round_trip(soft_preferences=("compare_train_and_flight",)),
        [_out("OUT-1", price="100"), _out("OUT-2", price="200", hour=16)],
        [_back("BACK-1")],
    )
    assert {item.outbound.mode for item in options} == {TransportMode.FLIGHT}


def _fields(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        "arrive_by": datetime(2026, 8, 20, 18, 0, tzinfo=SH),
        "return_after": None,
        "return_before": None,
        "booking_scope": BookingScope.OUTBOUND_ONLY,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "hard_constraints": ("direct_only",),
        "soft_preferences": (),
    }
    payload.update(overrides)
    return payload


def test_a_leg_index_that_does_not_exist_is_a_conflict() -> None:
    validation = validate_trip_request_values(
        _fields(scoped_hard_constraints=(ScopedRequirement("direct_only", 1),))
    )
    assert any("leg 1" in item for item in validation.conflicts)


def test_a_whole_journey_preference_cannot_be_pinned_to_one_leg() -> None:
    validation = validate_trip_request_values(
        _fields(
            soft_preferences=("hotel_near_client",),
            scoped_soft_preferences=(ScopedRequirement("hotel_near_client", 0),),
        )
    )
    assert any("whole journey" in item for item in validation.conflicts)


def test_the_two_views_must_name_the_same_requirements() -> None:
    """扁平列表仍然是政策引擎和旧载荷读到的东西，两边讲不同的话就是 bug。"""
    validation = validate_trip_request_values(
        _fields(scoped_hard_constraints=(ScopedRequirement("train_only"),))
    )
    assert any("same requirements" in item for item in validation.conflicts)
