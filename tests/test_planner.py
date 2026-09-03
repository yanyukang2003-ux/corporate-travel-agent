from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from corporate_travel_agent.domain.enums import PolicyOutcome, TransportMode
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    TransportOffer,
    TripRequestVersion,
)
from corporate_travel_agent.planning.planner import ItineraryPlanner

SH = ZoneInfo("Asia/Shanghai")


def _policy() -> PolicySnapshot:
    return PolicySnapshot(
        snapshot_id="planner-policy-v1",
        policy_version="planner-v1",
        level_rules={"L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        hotel_city_caps={"Shanghai": Decimal("2000")},
        arrival_buffer_minutes=30,
        exception_allowed_rule_ids=frozenset(),
        effective_from=date(2026, 1, 1),
        currency="CNY",
    )


def _employee() -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="planner-employee-v1",
        employee_id="E-PLAN",
        level="L1",
        department="Product",
        home_city="Beijing",
        manager_id="M-PLAN",
    )


def _request(**overrides: object) -> TripRequestVersion:
    payload: dict[str, object] = {
        "task_id": "planner-task",
        "version": 1,
        "traveler_id": "E-PLAN",
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": datetime(2026, 8, 20, 8, 0, tzinfo=SH),
        "arrive_by": datetime(2026, 8, 20, 18, 0, tzinfo=SH),
        "return_after": datetime(2026, 8, 21, 13, 0, tzinfo=SH),
        "return_before": datetime(2026, 8, 21, 22, 0, tzinfo=SH),
        "hotel_check_in": date(2026, 8, 20),
        "hotel_check_out": date(2026, 8, 21),
        "hard_constraints": ("hotel_required",),
        "soft_preferences": ("hotel_near_client",),
    }
    payload.update(overrides)
    return TripRequestVersion.from_flat(**payload)  # type: ignore[arg-type]


def _flight(ref_id: str, *, hour: int = 14, price: str = "35") -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="snap-flight",
        provider="duffel",
        mode=TransportMode.FLIGHT,
        origin="Beijing",
        destination="Shanghai",
        depart_at=datetime(2026, 8, 20, hour, 0, tzinfo=SH),
        arrive_at=datetime(2026, 8, 20, hour + 2, 0, tzinfo=SH),
        price=Decimal(price),
        seat_class="ECONOMY",
        currency="CNY",
    )


def _return_flight(ref_id: str = "RET-1") -> TransportOffer:
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="snap-flight",
        provider="duffel",
        mode=TransportMode.FLIGHT,
        origin="Shanghai",
        destination="Beijing",
        depart_at=datetime(2026, 8, 21, 15, 0, tzinfo=SH),
        arrive_at=datetime(2026, 8, 21, 17, 0, tzinfo=SH),
        price=Decimal("35"),
        seat_class="ECONOMY",
        currency="CNY",
    )


def _hotel(
    ref_id: str,
    *,
    name: str = "Cambria Hotel",
    nightly_price: str = "120.80",
    commute_minutes: int = COMMUTE_UNKNOWN_MINUTES,
) -> HotelOffer:
    return HotelOffer(
        ref_id=ref_id,
        snapshot_id="snap-hotel",
        provider="liteapi",
        name=name,
        city="Shanghai",
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 21),
        nightly_price=Decimal(nightly_price),
        commute_minutes=commute_minutes,
        currency="CNY",
    )


# 所有报价都在这个时刻之后：可行性校验会拒绝已经起飞的班次。
NOW = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)


def test_identical_hotel_rates_do_not_fill_the_recommendation_list() -> None:
    planner = ItineraryPlanner()
    options = planner.plan(
        _request(),
        _employee(),
        _policy(),
        leg_offers=[
            [_flight("OUT-A", hour=14), _flight("OUT-B", hour=16, price="80")],
            [_return_flight()],
        ],
        hotel_offers=[
            _hotel("litehotel_aaa"),
            _hotel("litehotel_bbb"),
            _hotel("litehotel_ccc"),
        ],
        limit=3,
        now=NOW,
    )

    hotel_names = {option.hotel.name for option in options if option.hotel}
    outbound_ids = [option.outbound.ref_id for option in options]
    assert len(options) == 2
    assert hotel_names == {"Cambria Hotel"}
    assert outbound_ids == ["OUT-A", "OUT-B"]


def test_unknown_commute_does_not_apply_near_client_penalty() -> None:
    planner = ItineraryPlanner()
    unknown = planner.plan(
        _request(),
        _employee(),
        _policy(),
        leg_offers=[[_flight("OUT-A")], [_return_flight()]],
        hotel_offers=[_hotel("litehotel_unknown")],
        limit=1,
        now=NOW,
    )
    known_far = planner.plan(
        _request(),
        _employee(),
        _policy(),
        leg_offers=[[_flight("OUT-A")], [_return_flight()]],
        hotel_offers=[_hotel("litehotel_far", commute_minutes=90)],
        limit=1,
        now=NOW,
    )

    assert unknown[0].preference_penalty == Decimal("0")
    assert "commute_minutes=unknown" in unknown[0].explanation_facts
    assert known_far[0].preference_penalty == Decimal("120")


def test_distinct_hotels_remain_available_as_separate_options() -> None:
    planner = ItineraryPlanner()
    options = planner.plan(
        _request(),
        _employee(),
        _policy(),
        leg_offers=[[_flight("OUT-A")], [_return_flight()]],
        hotel_offers=[
            _hotel("H1", name="River Hotel", nightly_price="400"),
            _hotel("H2", name="Garden Hotel", nightly_price="500"),
            _hotel("H3", name="Park Hotel", nightly_price="600"),
        ],
        limit=3,
        now=NOW,
    )

    assert [option.hotel.name for option in options if option.hotel] == [
        "River Hotel",
        "Garden Hotel",
        "Park Hotel",
    ]


def test_an_already_departed_flight_is_never_offered() -> None:
    """下午两点搜今天的票，上午九点那班已经飞了——不能出现在方案里。

    请求窗口只说明旅行者能接受什么，不说明现在还赶不赶得上：`departure_after`
    是下界，早上九点的航班满足"今天出发"，但此刻已经起飞。只有和当前时刻比才看得出。
    """
    planner = ItineraryPlanner()
    afternoon = datetime(2026, 8, 20, 14, 0, tzinfo=SH)

    options = planner.plan(
        _request(),
        _employee(),
        _policy(),
        leg_offers=[[_flight("MORNING", hour=9), _flight("LATER", hour=15)], [_return_flight()]],
        hotel_offers=[_hotel("litehotel_a")],
        limit=3,
        now=afternoon,
    )

    offered = {item.outbound.ref_id for item in options}
    assert "MORNING" not in offered
    assert "LATER" in offered


def test_a_departure_exactly_now_is_treated_as_gone() -> None:
    planner = ItineraryPlanner()
    depart = datetime(2026, 8, 20, 9, 0, tzinfo=SH)

    options = planner.plan(
        _request(),
        _employee(),
        _policy(),
        leg_offers=[[_flight("ON-THE-DOT", hour=9)], [_return_flight()]],
        hotel_offers=[_hotel("litehotel_a")],
        limit=3,
        now=depart,
    )

    assert options == []


def _approval_policy() -> PolicySnapshot:
    """舱位违规可以走例外审批。"""
    return replace(
        _policy(),
        exception_allowed_rule_ids=frozenset({"transport.flight.seat_class"}),
    )


def test_a_cheap_option_needing_approval_never_outranks_a_compliant_one() -> None:
    """政策是三档分类，不是可以被价格投票推翻的权重。

    此前政策被折算成 1000 元罚分加进分数，于是一个需审批但便宜得多的方案
    会排在完全合规的方案前面。现在改成先按政策分档、档内再排分数。
    """
    planner = ItineraryPlanner()
    compliant = _flight("COMPLIANT-PRICEY", hour=14, price="5000")
    needs_approval = replace(
        _flight("CHEAP-BUSINESS", hour=15, price="30"), seat_class="BUSINESS"
    )

    options = planner.plan(
        _request(),
        _employee(),
        _approval_policy(),
        leg_offers=[[compliant, needs_approval], [_return_flight()]],
        hotel_offers=[_hotel("litehotel_a")],
        limit=5,
        now=NOW,
    )

    refs = [item.outbound.ref_id for item in options]
    assert refs[:2] == ["COMPLIANT-PRICEY", "CHEAP-BUSINESS"], refs
    assert options[0].policy_decision.outcome is PolicyOutcome.COMPLIANT
    assert options[1].policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    # 便宜得多（分数更低），但仍然排在合规方案之后——政策不参与打分。
    assert options[1].score < options[0].score


def test_a_blocked_option_is_kept_with_its_reason_when_it_would_have_won() -> None:
    """被政策挡住的方案不能静默消失——否则用户只看到一份莫名偏贵的列表。"""
    planner = ItineraryPlanner()
    allowed = _flight("ALLOWED", hour=14, price="900")
    blocked = replace(_flight("BLOCKED-CHEAPEST", hour=15, price="20"), seat_class="BUSINESS")

    options = planner.plan(
        _request(),
        _employee(),
        _policy(),  # 不允许例外 → 舱位违规是禁止而非需审批
        leg_offers=[[allowed, blocked], [_return_flight()]],
        hotel_offers=[_hotel("litehotel_a")],
        limit=3,
        now=NOW,
    )

    refs = [item.outbound.ref_id for item in options]
    assert "ALLOWED" in refs
    assert "BLOCKED-CHEAPEST" in refs, "最便宜的那个被挡住了，必须让用户看得见"
    blocked_option = next(
        item for item in options if item.outbound.ref_id == "BLOCKED-CHEAPEST"
    )
    assert blocked_option.policy_decision.outcome is not PolicyOutcome.COMPLIANT
    assert blocked_option.policy_decision.violation_ids, "必须带着被挡的理由"
    assert refs.index("BLOCKED-CHEAPEST") == len(refs) - 1, "排在所有可选方案之后"


def test_a_blocked_option_that_is_worse_anyway_is_not_shown() -> None:
    """保留是为了透明，不是为了凑数：比可选方案还差的被挡方案只是噪音。"""
    planner = ItineraryPlanner()
    allowed = _flight("ALLOWED-CHEAP", hour=14, price="20")
    blocked = replace(_flight("BLOCKED-PRICEY", hour=15, price="9000"), seat_class="BUSINESS")

    options = planner.plan(
        _request(),
        _employee(),
        _policy(),
        leg_offers=[[allowed, blocked], [_return_flight()]],
        hotel_offers=[_hotel("litehotel_a")],
        limit=3,
        now=NOW,
    )

    refs = [item.outbound.ref_id for item in options]
    assert "ALLOWED-CHEAP" in refs
    assert "BLOCKED-PRICEY" not in refs
