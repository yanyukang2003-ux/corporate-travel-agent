#!/usr/bin/env python3
"""量一次：多城行程**分段单独问价** vs **一次多段请求**，差多少钱。

## 为什么先量再改

方案第 06 步的开工前测量，规矩见 HANDOFF §30.6：**不要凭一段描述就开工。**

今天每段各发一次搜索，等于买 N 张单程票。而 Duffel / Amadeus 把多城当**一次请求**
（多个 slice，返回**一个**覆盖全部航段的报价）——开口票按 IATA 票价构造规则定价，
**不是几张单程相加**。这一步值不值得做，取决于两者到底差多少。

**名词**（先解释再用）：

- **slice**：Duffel 的说法，一次起讫。往返 = 2 个 slice，三段多城 = 3 个。
- **分段问价**：今天的做法。每段各发一次请求，每段各取最便宜的，加起来。
- **一次多段**：一次请求放 N 个 slice，供应商返回覆盖全程的整票报价。

## 结果怎么读

- **一次多段更便宜** → 第 06 步直接省钱，做
- **差不多** → 这一步只剩"少发几次请求"，价值有限，可以往后排
- **一次多段更贵或搜不到** → 说明沙箱的多段库存不真实，**别拿它当结论**，
  要么换真实环境验，要么这一步就不该现在做

**这个脚本会真的联网**：对 Duffel 沙箱只读查询。一条行程 = N 次分段请求 + 1 次多段
请求。不下单、不付款、不写任何东西。不调用任何语言模型。

```bash
set -a && . ./.env && set +a
.venv/bin/python examples/measure_multicity_pricing.py \\
  --output reports/evaluation-runs/multicity-pricing-NEWDIR \\
  --confirm-live-provider-calls
```
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import sleep
from typing import Any

from corporate_travel_agent.providers.base import ProviderError, TransportSearchQuery
from corporate_travel_agent.providers.duffel import DuffelProvider

REPO = Path(__file__).resolve().parents[1]
RUNNER_VERSION = "multicity-pricing-measure-v1"


@dataclass(frozen=True, slots=True)
class Journey:
    """一条多城行程：按走的顺序给出每一段和它出发那天。"""

    journey_id: str
    title: str
    #: [(起, 讫, 出发日偏移天数), ...]。偏移从 --depart-in 那天算起，
    #: 用相对天数是为了这个脚本隔多久跑都还是"未来的行程"。
    legs: tuple[tuple[str, str, int], ...]


JOURNEYS: tuple[Journey, ...] = (
    Journey(
        journey_id="MP-01",
        title="北京 → 上海 → 杭州 → 北京（国内三段）",
        legs=(("PEK", "SHA", 0), ("SHA", "HGH", 3), ("HGH", "PEK", 4)),
    ),
    Journey(
        journey_id="MP-02",
        title="北京 → 东京 → 新加坡 → 北京（跨境三段）",
        legs=(("PEK", "TYO", 0), ("TYO", "SIN", 3), ("SIN", "PEK", 6)),
    ),
    Journey(
        journey_id="MP-03",
        title="上海 → 香港 → 曼谷 → 上海（跨境三段）",
        legs=(("SHA", "HKG", 0), ("HKG", "BKK", 2), ("BKK", "SHA", 5)),
    ),
    Journey(
        journey_id="MP-04",
        title="**对照组：普通往返**（北京 ⇄ 上海）",
        legs=(("PEK", "SHA", 0), ("SHA", "PEK", 3)),
    ),
)


def _cheapest_of(offers: list[dict[str, Any]]) -> tuple[Decimal, str] | None:
    """最便宜的那条报价与它的币种；没有报价时返回 None。"""
    priced: list[tuple[Decimal, str]] = []
    for offer in offers:
        amount = offer.get("total_amount")
        currency = offer.get("total_currency")
        if amount is None or currency is None:
            continue
        try:
            priced.append((Decimal(str(amount)), str(currency)))
        except (ArithmeticError, ValueError):
            continue
    return min(priced, key=lambda item: item[0]) if priced else None


def _leg_by_leg(
    provider: DuffelProvider, journey: Journey, start: date, pause: float
) -> dict[str, Any]:
    """今天的做法：每段各发一次搜索，各取最便宜，加起来。"""
    rows: list[dict[str, Any]] = []
    total = Decimal("0")
    currencies: set[str] = set()
    complete = True
    for index, (origin, destination, offset) in enumerate(journey.legs):
        if index:
            sleep(pause)
        depart = start + timedelta(days=offset)
        query = TransportSearchQuery(
            origin=origin,
            destination=destination,
            depart_after=datetime(depart.year, depart.month, depart.day, tzinfo=UTC),
            arrive_before=None,
        )
        try:
            snapshot = provider.search_transport(query)
        except ProviderError as exc:
            rows.append({"leg": f"{origin}-{destination}", "error": str(exc)[:180]})
            complete = False
            continue
        offers = [
            {"total_amount": str(item.price), "total_currency": item.currency}
            for item in snapshot.items
        ]
        cheapest = _cheapest_of(offers)
        if cheapest is None:
            rows.append({"leg": f"{origin}-{destination}", "offers": 0})
            complete = False
            continue
        amount, currency = cheapest
        total += amount
        currencies.add(currency)
        rows.append(
            {
                "leg": f"{origin}-{destination}",
                "offers": len(offers),
                "cheapest": str(amount),
                "currency": currency,
            }
        )
    return {
        "legs": rows,
        "complete": complete and len(currencies) == 1,
        "total": str(total) if complete and len(currencies) == 1 else None,
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "requests": len(journey.legs),
    }


def _one_request(
    provider: DuffelProvider, journey: Journey, start: date
) -> dict[str, Any]:
    """Duffel 的做法：一次请求放 N 个 slice，返回覆盖全程的整票报价。

    直接拼 payload 走 provider 的 HTTP 通道——**这一步还没有正式接口**，
    有没有接口正是这次测量要回答的事。
    """
    payload = {
        "data": {
            "cabin_class": "economy",
            "slices": [
                {
                    "origin": origin,
                    "destination": destination,
                    "departure_date": (start + timedelta(days=offset)).isoformat(),
                }
                for origin, destination, offset in journey.legs
            ],
            "passengers": [{"type": "adult"}],
        }
    }
    try:
        response = provider._send(  # noqa: SLF001 - 测量脚本，正式接口正待这次结论
            "POST",
            "/air/offer_requests",
            params={"return_offers": "true", "supplier_timeout": "20000"},
            json_body=payload,
        )
        body = provider._successful_payload(response)  # noqa: SLF001
    except ProviderError as exc:
        return {"error": str(exc)[:180], "requests": 1}
    offers = body.get("data", {}).get("offers") or []
    # 只收**真的覆盖了每一段**的报价。少一段的整票不能拿来比价——
    # 那是另一趟行程，便宜是应该的。
    full = [
        offer
        for offer in offers
        if isinstance(offer.get("slices"), list)
        and len(offer["slices"]) == len(journey.legs)
    ]
    cheapest = _cheapest_of(full)
    return {
        "offers": len(offers),
        "offers_covering_every_leg": len(full),
        "cheapest": str(cheapest[0]) if cheapest else None,
        "currency": cheapest[1] if cheapest else None,
        "requests": 1,
    }


def _compare(split: dict[str, Any], whole: dict[str, Any]) -> dict[str, Any]:
    """两种问法的差价。**任一边没问全就不下结论。**"""
    if split.get("total") is None or whole.get("cheapest") is None:
        return {"comparable": False, "reason": "至少有一边没拿到覆盖全程的报价"}
    if split.get("currency") != whole.get("currency"):
        return {"comparable": False, "reason": "两边币种不同，不能直接相减"}
    split_total = Decimal(split["total"])
    whole_total = Decimal(whole["cheapest"])
    delta = whole_total - split_total
    return {
        "comparable": True,
        "split_total": str(split_total),
        "one_request_total": str(whole_total),
        "delta": str(delta),
        "delta_pct": (
            float(delta / split_total * 100) if split_total else None
        ),
        "cheaper": "one_request" if delta < 0 else ("split" if delta > 0 else "same"),
    }


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 多城报价：分段问价 vs 一次多段请求",
        "",
        f"- measured_at: `{payload['measured_at']}`",
        f"- runner: `{payload['runner_version']}`",
        f"- 出发日: `{payload['depart_on']}`（相对今天 +{payload['depart_in_days']} 天）",
        f"- Duffel 沙箱只读查询 **{payload['duffel_requests']} 次**，0 次模型调用",
        "",
        "| 行程 | 分段合计 | 一次多段 | 差价 | 谁更便宜 |",
        "|---|---:|---:|---:|---|",
    ]
    for row in payload["journeys"]:
        cmp = row["comparison"]
        if not cmp.get("comparable"):
            lines.append(
                f"| {row['journey_id']} | — | — | — | **没法比**：{cmp.get('reason')} |"
            )
            continue
        currency = row["split"]["currency"]
        pct = cmp["delta_pct"]
        who = {
            "one_request": "**一次多段**",
            "split": "分段",
            "same": "一样",
        }[cmp["cheaper"]]
        lines.append(
            f"| {row['journey_id']} | {cmp['split_total']} {currency} "
            f"| {cmp['one_request_total']} {currency} "
            f"| {cmp['delta']}（{pct:+.1f}%） | {who} |"
        )
    lines.extend(["", "## 逐条", ""])
    for row in payload["journeys"]:
        lines.append(f"### {row['journey_id']} · {row['title']}")
        lines.append("")
        lines.append(f"- 分段问价：{json.dumps(row['split'], ensure_ascii=False)}")
        lines.append(f"- 一次多段：{json.dumps(row['one_request'], ensure_ascii=False)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--depart-in", type=int, default=45, help="出发日距今多少天")
    parser.add_argument("--pause-seconds", type=float, default=1.5)
    parser.add_argument(
        "--confirm-live-provider-calls",
        action="store_true",
        required=True,
        help="确认这次会真的对 Duffel 沙箱发起只读查询",
    )
    args = parser.parse_args()

    output = args.output
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)

    provider = DuffelProvider.from_environment()
    start = date.today() + timedelta(days=args.depart_in)
    rows: list[dict[str, Any]] = []
    for index, journey in enumerate(JOURNEYS):
        if index:
            sleep(args.pause_seconds)
        split = _leg_by_leg(provider, journey, start, args.pause_seconds)
        sleep(args.pause_seconds)
        whole = _one_request(provider, journey, start)
        rows.append(
            {
                "journey_id": journey.journey_id,
                "title": journey.title,
                "legs": [f"{a}-{b}" for a, b, _ in journey.legs],
                "split": split,
                "one_request": whole,
                "comparison": _compare(split, whole),
            }
        )

    payload = {
        "measured_at": datetime.now(UTC).isoformat(),
        "runner_version": RUNNER_VERSION,
        "billed_model_calls": 0,
        "duffel_requests": sum(
            row["split"]["requests"] + row["one_request"]["requests"] for row in rows
        ),
        "depart_in_days": args.depart_in,
        "depart_on": start.isoformat(),
        "journeys": rows,
    }
    (output / "measurement.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = _markdown(payload)
    (output / "REPORT.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"报告 → {output}")


if __name__ == "__main__":
    main()
