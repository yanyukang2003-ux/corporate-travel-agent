#!/usr/bin/env python3
"""量一次：规划器不做笛卡尔积之后，快了多少、少组装了多少组。

**名词**（先解释再用）：

- **报价组合**：每一段各挑一条报价、再挑一家酒店，凑成的一整套。
  规划器要把每一组"组装"成一条方案（判可行性、跑政策、算分），这一步是成本大头。
- **走法**：这趟差旅怎么走——每段坐飞机还是高铁、住不住。
  14 点那班和 16 点那班不是两种走法，是同一种走法的两个价格。
- **政策分档**：合规 / 需审批 / 不可选。排序先分档，档内再比分数。

改动之前是「每段报价做全组合，逐组组装」，组合数是 |报价|^段数。
改动之后是「每种走法 × 每个政策档 × 每个目标 → 每一项各取最优」，
和报价数成正比而不是相乘。

这个脚本只在**两段往返**上实测——三段以上今天还到不了规划器（语义层与结果侧
仍是两段形状，见方案第 02、03 步）。所以三段以上那几行是**外推**，明确标出来。

不调用任何模型，不访问外网，不花钱。
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import product
from pathlib import Path
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

#: Duffel 适配器每段最多归一化多少条报价（`providers/duffel.py` 的 max_offers 默认值）。
DUFFEL_OFFERS_PER_LEG = 50
#: 一次搜索让人等到这里已经是上限。
ACCEPTABLE_SECONDS = 2.0


def _policy() -> PolicySnapshot:
    return PolicySnapshot(
        snapshot_id="measure-policy",
        policy_version="measure-v1",
        level_rules={"L1": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        hotel_city_caps={"Shanghai": Decimal("900")},
        arrival_buffer_minutes=30,
        exception_allowed_rule_ids=frozenset({"transport.flight.seat_class"}),
        effective_from=date(2026, 1, 1),
        currency="CNY",
    )


def _employee() -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="measure-employee",
        employee_id="E-M",
        level="L1",
        department="Product",
        home_city="Beijing",
        manager_id="M-M",
    )


def _request(*, lodging: bool) -> TripRequestVersion:
    return TripRequestVersion(
        task_id="measure-task",
        version=1,
        traveler_id="E-M",
        origin="Beijing",
        destination="Shanghai",
        departure_after=datetime(2026, 8, 20, 5, 0, tzinfo=SH),
        arrive_by=datetime(2026, 8, 20, 23, 30, tzinfo=SH),
        return_after=datetime(2026, 8, 21, 5, 0, tzinfo=SH),
        return_before=datetime(2026, 8, 21, 23, 30, tzinfo=SH),
        hotel_check_in=date(2026, 8, 20) if lodging else None,
        hotel_check_out=date(2026, 8, 21) if lodging else None,
        hard_constraints=("hotel_required",) if lodging else (),
        soft_preferences=("avoid_early_departure",),
        booking_scope=BookingScope.ROUND_TRIP,
    )


def _offers(
    rng: random.Random, prefix: str, origin: str, destination: str, day: int, count: int
) -> list[TransportOffer]:
    offers: list[TransportOffer] = []
    for index in range(count):
        mode = TransportMode.FLIGHT if index % 3 else TransportMode.TRAIN
        hour = 6 + (index % 12)
        hours = 1 + (index % 4)
        offers.append(
            TransportOffer(
                ref_id=f"{prefix}-{index:03d}",
                snapshot_id="measure-snap",
                provider="mock",
                mode=mode,
                origin=origin,
                destination=destination,
                depart_at=datetime(2026, 8, day, hour, 0, tzinfo=SH),
                arrive_at=datetime(2026, 8, day, hour + hours, 0, tzinfo=SH),
                price=Decimal(str(300 + rng.randrange(0, 40) * 20)),
                seat_class=(
                    ("ECONOMY" if index % 5 else "BUSINESS")
                    if mode is TransportMode.FLIGHT
                    else "SECOND_CLASS"
                ),
                currency="CNY",
            )
        )
    return offers


def _hotels(count: int) -> list[HotelOffer]:
    return [
        HotelOffer(
            ref_id=f"H-{index:03d}",
            snapshot_id="measure-snap",
            provider="mock",
            name=f"Hotel {index}",
            city="Shanghai",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
            nightly_price=Decimal(str(600 + index * 25)),
            commute_minutes=10 + index,
            currency="CNY",
        )
        for index in range(count)
    ]


def _exhaustive_plan(
    planner: ItineraryPlanner,
    request: TripRequestVersion,
    employee: EmployeeProfileSnapshot,
    policy: PolicySnapshot,
    outbound_offers: list[TransportOffer],
    inbound_offers: list[TransportOffer],
    hotel_offers: list[HotelOffer],
    limit: int,
) -> list[TravelOptionVersion]:
    """改动之前的候选产生方式，留作对照。"""
    pools = [list(outbound_offers), list(inbound_offers)]
    hotel_choices: list[HotelOffer | None] = (
        list(hotel_offers) if request.hotel_check_in is not None else [None]
    )
    minutes_per_unit = duration_minutes_per_unit(request)
    candidates: list[tuple[object, TravelOptionVersion]] = []
    for shape in _enumerate_shapes(pools, hotel_choices):
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
                hotel,
                shape,
                minutes_per_unit,
                now=NOW,
            )
            if option is not None:
                candidates.append((shape, option))
    return _select_options(
        candidates, limit, compare_modes=wants_mode_comparison(request)
    )


def _counted(planner: ItineraryPlanner) -> tuple[ItineraryPlanner, list[int]]:
    """给规划器套一层计数，好知道它到底组装了多少组。"""
    counter = [0]
    original = planner._evaluate

    def counting(*args: object, **kwargs: object) -> object:
        counter[0] += 1
        return original(*args, **kwargs)

    planner._evaluate = counting  # type: ignore[method-assign]
    return planner, counter


def measure_two_legs() -> list[dict[str, object]]:
    """两段往返，逐步加大每段报价数，实测新旧两条路径。"""
    rows: list[dict[str, object]] = []
    for per_leg in (10, 20, 40, 50, 80):
        rng = random.Random(4242)
        request = _request(lodging=True)
        policy = _policy()
        employee = _employee()
        outbound = _offers(rng, "OUT", "Beijing", "Shanghai", 20, per_leg)
        inbound = _offers(rng, "RET", "Shanghai", "Beijing", 21, per_leg)
        hotels = _hotels(4)

        new_planner, new_calls = _counted(ItineraryPlanner())
        start = time.perf_counter()
        new_options = new_planner.plan(
            request,
            employee,
            policy,
            leg_offers=[outbound, inbound],
            hotel_offers=hotels,
            limit=3,
            now=NOW,
        )
        new_seconds = time.perf_counter() - start

        old_planner, old_calls = _counted(ItineraryPlanner())
        start = time.perf_counter()
        old_options = _exhaustive_plan(
            old_planner, request, employee, policy, outbound, inbound, hotels, 3
        )
        old_seconds = time.perf_counter() - start

        rows.append(
            {
                "offers_per_leg": per_leg,
                "hotels": len(hotels),
                "exhaustive_combinations_evaluated": old_calls[0],
                "generator_combinations_evaluated": new_calls[0],
                "exhaustive_seconds": round(old_seconds, 4),
                "generator_seconds": round(new_seconds, 4),
                "speedup": round(old_seconds / new_seconds, 1) if new_seconds else None,
                "same_options": [item.option_id for item in old_options]
                == [item.option_id for item in new_options],
            }
        )
    return rows


def project_more_legs(per_combination_seconds: float) -> list[dict[str, object]]:
    """三段以上是外推，不是实测——今天还到不了规划器。"""
    rows: list[dict[str, object]] = []
    for legs in range(2, 7):
        combinations = DUFFEL_OFFERS_PER_LEG**legs
        seconds = combinations * per_combination_seconds
        rows.append(
            {
                "legs": legs,
                "measured": legs == 2,
                "exhaustive_combinations": combinations,
                "exhaustive_seconds": round(seconds, 3),
                "within_acceptable": seconds <= ACCEPTABLE_SECONDS,
            }
        )
    return rows


def render(report: dict[str, object]) -> str:
    two = report["two_leg_measured"]
    assert isinstance(two, list)
    projected = report["exhaustive_projection"]
    assert isinstance(projected, list)

    lines = [
        "# 规划器去笛卡尔积：测量报告",
        "",
        "**结论先说。** 两段往返上，新旧两条路径摆出来的方案**完全一致**，",
        "但要组装的报价组合从与报价数**相乘**变成与报价数**成正比**。",
        "",
        "**名词**：*报价组合* = 每段各挑一条报价、再挑一家酒店凑成的一整套；",
        "规划器要把每一组组装成方案（判可行性、跑政策、算分），这是成本大头。",
        "",
        "## 实测：两段往返（每段报价数递增，4 家酒店，limit=3）",
        "",
        "| 每段报价 | 穷举组装 | 新法组装 | 穷举耗时 | 新法耗时 | 快了 | 结果一致 |",
        "|---:|---:|---:|---:|---:|---:|:--:|",
    ]
    for row in two:
        lines.append(
            f"| {row['offers_per_leg']} | {row['exhaustive_combinations_evaluated']} "
            f"| {row['generator_combinations_evaluated']} "
            f"| {row['exhaustive_seconds']} s | {row['generator_seconds']} s "
            f"| {row['speedup']}× | {'是' if row['same_options'] else '**否**'} |"
        )

    lines += [
        "",
        "## 外推：穷举在更多段上要跑多久",
        "",
        f"按 Duffel 每段 {DUFFEL_OFFERS_PER_LEG} 条报价（`duffel.py` 的 max_offers 默认值），",
        "用上表实测出的「每组合耗时」外推。**三段以上今天到不了规划器**——语义层与",
        "结果侧仍是两段形状（方案第 02、03 步），所以这几行是外推，不是实测。",
        "这张表只数交通段，没算住宿那一轴；真要住店，每一行还要再乘上酒店数。",
        "",
        "| 航段 | 报价组合数 | 穷举耗时 | 2 秒内 | 来源 |",
        "|---:|---:|---:|:--:|:--|",
    ]
    for row in projected:
        lines.append(
            f"| {row['legs']} | {row['exhaustive_combinations']:,} "
            f"| {row['exhaustive_seconds']} s "
            f"| {'是' if row['within_acceptable'] else '**否**'} "
            f"| {'实测' if row['measured'] else '外推'} |"
        )

    lines += [
        "",
        "## 这一步改的是什么",
        "",
        "**不是性能优化，是可行性。** 两段上省下的那点时间无关紧要；",
        "要紧的是穷举在第 4 段就已经跑不完，而多城行程从第 3 段开始。",
        "",
        "新法的正确性靠三件事撑着，都不是这里的主张，是既有代码本来就成立的性质：",
        "",
        "1. **分数、价格、时长都逐项相加**——每段各自的贡献加起来就是整条方案的。",
        "2. **可行性逐项独立**——每条检查都拿报价和*请求窗口*比，从不和另一段的实际报价比。",
        "3. **政策档是逐项取最差**——所以「整条不超过某档」等价于「每项都不超过某档」。",
        "",
        "`tests/test_planner_generator_equivalence.py` 把穷举留作对照实现，",
        "在 220 个随机行程上断言两边摆出来的方案逐字段相同。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"输出目录必须不存在：{args.output}")

    two_leg = measure_two_legs()
    # 用最大那一档的实测值算「每组合耗时」，样本最足。
    largest = two_leg[-1]
    per_combination = float(largest["exhaustive_seconds"]) / int(
        largest["exhaustive_combinations_evaluated"]
    )
    report: dict[str, object] = {
        "two_leg_measured": two_leg,
        "seconds_per_combination": per_combination,
        "duffel_offers_per_leg": DUFFEL_OFFERS_PER_LEG,
        "exhaustive_projection": project_more_legs(per_combination),
    }

    args.output.mkdir(parents=True)
    (args.output / "measurement.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "REPORT.md").write_text(render(report), encoding="utf-8")
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
