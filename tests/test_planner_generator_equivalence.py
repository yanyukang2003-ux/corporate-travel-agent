"""第 01 步：规划器不再做笛卡尔积，摆出来的东西必须一模一样。

规划器此前是「每段报价做全组合 → 逐个评 → 选」。组合数是 |报价|^段数：
两段 50×50 尚可，六段 50⁶ 按本机实测每组合约 14 µs 要跑六十多个小时。
现在改成「每一项各取最优」——分数、价格、时长都是逐项相加的，可行性也逐项独立，
所以 `_select_options` 要读的那几个极值不必枚举组合就能算出来。

**这份文件把穷举实现留作对照。** 它故意不跟着规划器走：如果两边互相跟随，
这个测试就再也证明不了任何事（和 `evaluation_agent_eval` 里那份罚分参照实现同理）。
对照只替换**候选是怎么产生的**，组装与挑选仍走规划器自己的那一份，
好让差异只可能出自这次改动。
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import product
from zoneinfo import ZoneInfo

from corporate_travel_agent.domain.enums import BookingScope, TransportMode
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    TransportOffer,
    TravelOptionVersion,
    TripRequestVersion,
)
from corporate_travel_agent.planning.planner import (
    ItineraryPlanner,
    _enumerate_shapes,
    _select_options,
)
from corporate_travel_agent.planning.preferences import (
    duration_minutes_per_unit,
    wants_mode_comparison,
)

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _exhaustive_plan(
    planner: ItineraryPlanner,
    request: TripRequestVersion,
    employee: EmployeeProfileSnapshot,
    policy: PolicySnapshot,
    outbound_offers: list[TransportOffer],
    inbound_offers: list[TransportOffer],
    hotel_offers: list[HotelOffer],
    limit: int,
    *,
    now: datetime,
) -> list[TravelOptionVersion]:
    """改动之前的候选产生方式：把每段报价做全组合，逐个评。"""
    pools = [
        [
            offer
            for offer in outbound_offers
            if _leg_satisfies(request, 0, offer)
        ]
    ]
    if request.return_after is not None:
        pools.append(
            [offer for offer in inbound_offers if _leg_satisfies(request, 1, offer)]
        )
    hotel_choices: list[HotelOffer | None] = (
        list(hotel_offers) if request.hotel_check_in is not None else [None]
    )

    # 对照实现只覆盖 1–2 段、单处住宿——它证明的是"去笛卡尔积没有改变结果"，
    # 不是多城。多城的证人是 tests/test_multi_city_planning.py。
    stay_pools = (
        [[item for item in hotel_choices if item is not None]]
        if request.hotel_check_in is not None
        else []
    )

    minutes_per_unit = duration_minutes_per_unit(request)
    candidates: list[tuple[object, TravelOptionVersion]] = []
    for shape in _enumerate_shapes(pools, stay_pools):
        shaped_pools = [
            [offer for offer in pool if offer.mode is shape.modes[index]]
            for index, pool in enumerate(pools)
        ]
        shaped_hotels: list[HotelOffer | None] = (
            [item for item in hotel_choices if item is not None]
            if shape.with_lodging
            else [None]
        )
        for combination in product(*shaped_pools, shaped_hotels):
            *transports, hotel = combination
            option = planner._evaluate(
                request,
                employee,
                policy,
                list(transports),
                [hotel] if hotel is not None else [],
                shape,
                minutes_per_unit,
                now=now,
            )
            if option is not None:
                candidates.append((shape, option))

    return _select_options(
        candidates, limit, compare_modes=wants_mode_comparison(request)
    )


def _leg_satisfies(
    request: TripRequestVersion, leg_index: int, offer: TransportOffer
) -> bool:
    """对照用的硬要求筛选，与规划器同规则、独立一份。"""
    names = request.constraints_for_leg(leg_index)
    if "train_only" in names and offer.mode is not TransportMode.TRAIN:
        return False
    if "flight_only" in names and offer.mode is not TransportMode.FLIGHT:
        return False
    return not ("direct_only" in names and not offer.is_direct)


def _policy(rng: random.Random) -> PolicySnapshot:
    exceptions = rng.choice(
        [
            frozenset(),
            frozenset({"transport.flight.seat_class"}),
            frozenset({"transport.flight.seat_class", "hotel.city.nightly_cap"}),
        ]
    )
    return PolicySnapshot(
        snapshot_id="equiv-policy",
        policy_version="equiv-v1",
        level_rules={"L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        hotel_city_caps={"Shanghai": Decimal("900")},
        arrival_buffer_minutes=rng.choice([0, 30, 60]),
        exception_allowed_rule_ids=exceptions,
        effective_from=date(2026, 1, 1),
        currency="CNY",
    )


def _employee() -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="equiv-employee",
        employee_id="E-EQ",
        level="L1",
        department="Product",
        home_city="Beijing",
        manager_id="M-EQ",
    )


def _transport(
    rng: random.Random, ref_id: str, origin: str, destination: str, day: int
) -> TransportOffer:
    mode = rng.choice([TransportMode.FLIGHT, TransportMode.TRAIN])
    seat = (
        rng.choice(["ECONOMY", "BUSINESS"])
        if mode is TransportMode.FLIGHT
        else rng.choice(["SECOND_CLASS", "FIRST_CLASS"])
    )
    hour = rng.randrange(5, 20)
    duration = rng.randrange(1, 6)
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="equiv-snap",
        provider="mock",
        mode=mode,
        origin=origin,
        destination=destination,
        depart_at=datetime(2026, 8, day, hour, 0, tzinfo=SH),
        arrive_at=datetime(2026, 8, day, hour, 0, tzinfo=SH)
        + (datetime(2026, 1, 1, duration) - datetime(2026, 1, 1, 0)),
        # 价格故意大量重复，好把并列打破规则也逼出来。
        price=Decimal(rng.choice(["300", "300", "450", "620", "620", "880"])),
        seat_class=seat,
        available=rng.random() > 0.1,
        is_direct=rng.random() > 0.3,
        currency="CNY",
    )


def _hotel(rng: random.Random, ref_id: str) -> HotelOffer:
    return HotelOffer(
        ref_id=ref_id,
        snapshot_id="equiv-snap",
        provider="mock",
        # 名字也故意重复：展示上相同的两家酒店在结果里本来就会被合成一条。
        name=rng.choice(["Cambria", "Cambria", "Lanson Place"]),
        city="Shanghai",
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 21),
        nightly_price=Decimal(rng.choice(["700", "700", "950"])),
        commute_minutes=rng.choice([-1, 10, 45, 80]),
        available=rng.random() > 0.1,
        currency="CNY",
    )


def _request(rng: random.Random, *, round_trip: bool, lodging: bool) -> TripRequestVersion:
    constraints: list[str] = []
    if rng.random() > 0.7:
        constraints.append("direct_only")
    if rng.random() > 0.85:
        constraints.append("arrive_before_meeting")
    if lodging:
        constraints.append("hotel_required")
    preferences = [
        name
        for name in ("avoid_early_departure", "prefer_train", "hotel_near_client",
                     "lowest_cost", "shortest_duration", "compare_train_and_flight")
        if rng.random() > 0.72
    ]
    return TripRequestVersion(
        task_id="equiv-task",
        version=1,
        traveler_id="E-EQ",
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 20, 6, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 20, 23, 0, tzinfo=SH),
        return_after=datetime(2026, 8, 21, 6, 0, tzinfo=SH) if round_trip else None,
        return_before=datetime(2026, 8, 21, 23, 0, tzinfo=SH) if round_trip else None,
        hotel_check_in=date(2026, 8, 20) if lodging else None,
        hotel_check_out=date(2026, 8, 21) if lodging else None,
        hard_constraints=tuple(constraints),
        soft_preferences=tuple(preferences),
        booking_scope=BookingScope.ROUND_TRIP if round_trip else BookingScope.OUTBOUND_ONLY,
    )


def test_the_generator_puts_out_exactly_what_the_cartesian_product_did() -> None:
    """随机行程上，新旧两条产生路径摆出来的方案逐字段相同。"""
    planner = ItineraryPlanner()
    compared = 0
    non_empty = 0
    for seed in range(220):
        rng = random.Random(seed)
        round_trip = rng.random() > 0.35
        lodging = rng.random() > 0.4
        request = _request(rng, round_trip=round_trip, lodging=lodging)
        policy = _policy(rng)
        employee = _employee()
        outbound = [
            _transport(rng, f"OUT-{index}", "Beijing", "Shanghai", 20)
            for index in range(rng.randrange(1, 7))
        ]
        inbound = (
            [
                _transport(rng, f"RET-{index}", "Shanghai", "Beijing", 21)
                for index in range(rng.randrange(1, 7))
            ]
            if round_trip
            else []
        )
        hotels = (
            [_hotel(rng, f"H-{index}") for index in range(rng.randrange(1, 5))]
            if lodging
            else []
        )
        limit = rng.choice([1, 2, 3, 4])

        actual = planner.plan(
            request,
            employee,
            policy,
            leg_offers=[outbound, inbound],
            hotel_offers=hotels,
            limit=limit,
            now=NOW,
        )
        expected = _exhaustive_plan(
            planner,
            request,
            employee,
            policy,
            outbound,
            inbound,
            hotels,
            limit,
            now=NOW,
        )
        assert actual == expected, f"seed {seed} 上两条路径给出的方案不同"
        compared += 1
        if actual:
            non_empty += 1

    assert compared == 220
    # 全是空结果的话这个测试什么也没证明。门槛取"过半"，是为了守住样本强度，
    # 不是贴着当前数字定的——随机场景里出不了方案的那些（报价不可用、路线不符、
    # 时间窗对不上）本来就该占一部分。
    assert non_empty > 110, f"只有 {non_empty} 个场景真的出了方案，样本太弱"


def test_the_generator_stops_evaluating_the_whole_cartesian_product() -> None:
    """这一步的意义就在这里：组装次数不再随报价数相乘。"""
    rng = random.Random(9001)
    request = _request(rng, round_trip=True, lodging=False)
    policy = _policy(random.Random(0))
    outbound = [
        _transport(random.Random(100 + index), f"OUT-{index}", "Beijing", "Shanghai", 20)
        for index in range(40)
    ]
    inbound = [
        _transport(random.Random(200 + index), f"RET-{index}", "Shanghai", "Beijing", 21)
        for index in range(40)
    ]

    planner = ItineraryPlanner()
    calls = 0
    original = planner._evaluate

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    planner._evaluate = counting  # type: ignore[method-assign]
    planner.plan(
        request,
        _employee(),
        policy,
        leg_offers=[outbound, inbound],
        hotel_offers=[],
        limit=3,
        now=NOW,
    )

    # 穷举要组装 40×40=1600 组；按走法与政策档各取最优只需要几十组。
    assert calls < 100, f"仍然组装了 {calls} 组，说明没有真的摆脱笛卡尔积"
