#!/usr/bin/env python3
"""量一次：把「走法」当枚举对象之后，到底会枚举出多少种、要花多少次库存查询。

HANDOFF §30.6 明确要求「开工前必须先量一次，不要凭那段话就开工」。这个脚本就是那一次。

**名词**（先解释再用）：

- **走法**：这趟差旅**怎么走**——每一段坐飞机还是高铁、要不要住一晚。
  「同一个走法的不同价格」不算两种走法：14 点那班和 16 点那班是同一种走法。
- **航段**：一次起讫（北京→上海）。单程 1 段、往返 2 段、多城 N 段。
- **库存查询**：向 Provider（Duffel / LiteAPI）问一次「这条线这个时间有什么」。
  这是唯一真花钱、真限速的东西，所以它是这次测量的重点。

不调用任何模型，不访问外网，不花钱。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORLDS = ROOT / "data" / "evaluation" / "agent-eval-v1" / "fixtures" / "worlds.json"

MAX_JOURNEY_LEGS = 6


@dataclass(frozen=True, slots=True)
class ShapeCount:
    """某个行程形态下的枚举规模。"""

    legs: int
    modes_per_leg: tuple[int, ...]
    lodging_choices: int
    shapes: int
    inventory_queries: int


def count_shapes(
    modes_per_leg: tuple[int, ...], *, lodging_choices: int, searches_lodging: bool
) -> ShapeCount:
    """走法数 = 各段可选交通方式数的乘积 × 住宿的选择数。

    库存查询数**与走法数无关**：一段查一次就把这一段所有交通方式都拿回来了，
    要住宿才再查一次。这是这次测量最要紧的一个结论。
    """
    shapes = lodging_choices
    for count in modes_per_leg:
        shapes *= count
    return ShapeCount(
        legs=len(modes_per_leg),
        modes_per_leg=modes_per_leg,
        lodging_choices=lodging_choices,
        shapes=shapes,
        inventory_queries=len(modes_per_leg) + (1 if searches_lodging else 0),
    )


def today_combination_count(offers_per_leg: tuple[int, ...], hotels: int) -> int:
    """今天的做法：去程 × 返程 × 酒店的笛卡尔积，每一格都要完整评一遍。"""
    total = max(hotels, 1)
    for count in offers_per_leg:
        total *= count
    return total


def measure_synthetic() -> list[dict[str, object]]:
    """按段数扫一遍：最坏情况（每段两种交通方式都有货）下的枚举规模。"""
    rows: list[dict[str, object]] = []
    for legs in range(1, MAX_JOURNEY_LEGS + 1):
        for modes in (1, 2):
            # 住宿的三种情形：根本不住、必须住、可住可不住（后者才是真的多一种走法）。
            for label, lodging, searched in (
                ("no_lodging", 1, False),
                ("lodging_required", 1, True),
                ("lodging_optional", 2, True),
            ):
                counted = count_shapes(
                    tuple([modes] * legs),
                    lodging_choices=lodging,
                    searches_lodging=searched,
                )
                rows.append(
                    {
                        "legs": legs,
                        "modes_per_leg": modes,
                        "lodging": label,
                        "shapes": counted.shapes,
                        "inventory_queries": counted.inventory_queries,
                    }
                )
    return rows


def measure_fixture_worlds() -> dict[str, object]:
    """拿冻结评测数据里 60 个真实世界量一遍今天的笛卡尔积规模。"""
    payload = json.loads(WORLDS.read_text())
    worlds = payload["worlds"] if isinstance(payload, dict) else payload
    if isinstance(worlds, dict):
        worlds = list(worlds.values())

    combos: list[int] = []
    shapes: list[int] = []
    mode_counts: Counter[int] = Counter()
    for world in worlds:
        inventory = world.get("inventory", {})
        transports = inventory.get("transports", [])
        hotels = inventory.get("hotels", [])
        outbound = [item for item in transports if item.get("direction") == "outbound"]
        inbound = [item for item in transports if item.get("direction") == "inbound"]
        legs = [outbound] + ([inbound] if inbound else [])
        offers_per_leg = tuple(max(len(leg), 1) for leg in legs)
        combos.append(today_combination_count(offers_per_leg, len(hotels)))
        modes_per_leg = tuple(
            max(len({item.get("mode") for item in leg}), 1) for leg in legs
        )
        for count in modes_per_leg:
            mode_counts[count] += 1
        shapes.append(
            count_shapes(
                modes_per_leg,
                lodging_choices=2 if hotels else 1,
                searches_lodging=bool(hotels),
            ).shapes
        )

    return {
        "worlds": len(worlds),
        "today_combinations": {
            "min": min(combos),
            "max": max(combos),
            "mean": round(sum(combos) / len(combos), 2),
        },
        "plan_shapes": {
            "min": min(shapes),
            "max": max(shapes),
            "mean": round(sum(shapes) / len(shapes), 2),
        },
        "modes_available_per_leg": dict(sorted(mode_counts.items())),
    }


def render(report: dict[str, object]) -> str:
    fixture = report["fixture_worlds"]
    synthetic = report["synthetic_sweep"]
    worst = max(synthetic, key=lambda row: row["shapes"])
    lines = [
        "# 走法枚举规模测量",
        "",
        "HANDOFF §30.6 要求的「开工前先量一次」。不花钱，不调模型。",
        "",
        "## 结论（三句话）",
        "",
        f"1. **走法数很小。** 最坏情况（{MAX_JOURNEY_LEGS} 段，每段飞机高铁都有货，"
        f"住宿可选可不选）也只有 **{worst['shapes']} 种走法**，穷举毫无压力。",
        "2. **枚举走法不多花一次库存查询。** 一段查一次就把这段所有交通方式都拿回来了；"
        f"最坏情况仍然是 **{worst['inventory_queries']} 次查询**"
        "（每段一次 + 住宿一次），和今天完全一样。",
        "3. **要评的报价组合一个不少，也一个不多。** 走法是对同一批组合的**分组**，"
        f"不是筛选：冻结评测的 60 个世界里今天平均评 "
        f"{fixture['today_combinations']['mean']} 格、最多 "
        f"{fixture['today_combinations']['max']} 格，按走法分组之后总数不变。"
        "所以第 05 步改的是**摆出来的是什么**，不是快多少——别把它当性能优化。",
        "",
        "## 详细数字",
        "",
        "### 冻结评测数据的 60 个世界",
        "",
        "| 指标 | 最小 | 平均 | 最大 |",
        "|---|---:|---:|---:|",
        f"| 今天的组合数（笛卡尔积格子） | {fixture['today_combinations']['min']} | "
        f"{fixture['today_combinations']['mean']} | {fixture['today_combinations']['max']} |",
        f"| 走法数 | {fixture['plan_shapes']['min']} | "
        f"{fixture['plan_shapes']['mean']} | {fixture['plan_shapes']['max']} |",
        "",
        "每段实际拿得到几种交通方式（段数计数）："
        f"{fixture['modes_available_per_leg']}。",
        "",
        "> 读这一行要小心：真实 Provider（Duffel）**只返回航班**，高铁库存今天只存在于"
        "Mock 与评测夹具里。所以「按交通方式枚举走法」在真实链路上现在只有一种取值，"
        "价值要等接入铁路库存才兑现。",
        "",
        "### 按段数扫一遍（最坏情况）",
        "",
        "| 航段数 | 每段可选方式 | 住宿 | 走法数 | 库存查询次数 |",
        "|---:|---:|---|---:|---:|",
    ]
    for row in synthetic:
        lines.append(
            f"| {row['legs']} | {row['modes_per_leg']} | {row['lodging']} | "
            f"{row['shapes']} | {row['inventory_queries']} |"
        )
    three_leg = [row for row in synthetic if row["legs"] == 3]
    worst_three = max(three_leg, key=lambda row: row["shapes"])
    lines += [
        "",
        "## 直接回答 §30.6 的两个问题",
        "",
        f"- **三段行程会枚举出多少种走法？** 最多 {worst_three['shapes']} 种"
        "（三段各有飞机和高铁两种货，住宿可选可不选）。",
        f"- **每种走法要花多少次库存查询？** 0 次。整趟一共 "
        f"{worst_three['inventory_queries']} 次（每段一次 + 住宿一次），"
        "枚举走法完全发生在这些查询之后。",
        "",
        "## 因此",
        "",
        "第 05 步可以做：规模不是问题，也不会多花 Provider 的钱。",
        "真正要小心的是**它现在能兑现多少**——见上面那条关于 Duffel 只有航班的提醒。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"输出目录必须不存在：{args.output}")

    report = {
        "fixture_worlds": measure_fixture_worlds(),
        "synthetic_sweep": measure_synthetic(),
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
