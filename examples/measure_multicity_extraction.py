#!/usr/bin/env python3
"""量一次：真实模型在**三段及以上行程**上，把航段抽对了几段。

## 为什么先量再改

方案第 05 步（语义层出航段列表）是六步里**唯一要花钱的一步**。§32.7 立的规矩：
开工前先花约 $0.05 量一次抽取正确率，**这个数决定第 05 步是一周还是一个月**。

- 抽得准 → 剩下的活是宿主接线：校验、追问、编译成 `journey`
- 抽不准 → 先修提示词，甚至要换个问法（一段一段地问，而不是一次读完）

**别一上来就改提示词。先知道它现在错在哪。**

## 量什么

不跑整条编排链路，只调**语义理解这一层**，看它填出来的 `legs` 对不对。分五项各自记分，
因为它们坏起来的后果完全不同：

| 项 | 问的是 | 错了会怎样 |
|---|---|---|
| `count` | 段数对不对 | 少一段 = 有一程没人订；多一段 = 凭空多出一趟 |
| `route` | 每段起讫对不对 | **最危险的一项**：拿一条用户没要过的航线去搜库存（§28 的静默错搜） |
| `order` | 顺序对不对 | 时序颠倒，`_journey_conflicts` 会拦，但用户拿到的是一句莫名其妙的报错 |
| `windows` | 时间窗对不对 | 搜出来的班次不在旅行者能接受的时间里 |
| `stays` | 过夜城市对不对 | 多城 N-1 个过夜点，少一个就是一晚没地方住 |

**顺序单独记分**，是因为 arXiv 2510.24719 说模型编行程的时序错误随城市数上升——
这正是要量的东西，不是可以假设掉的东西。

## 花什么钱

只调语言模型，**一次库存都不查**，不碰 Duffel / LiteAPI。
调用次数 = 用例数 × repeats。

```bash
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/measure_multicity_extraction.py \\
  --output reports/evaluation-runs/multicity-extraction-NEWDIR \\
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \\
  --repeats 3 --confirm-billable-model-calls
```

**单跑一次是噪声**，用 `--repeats` 区分真实能力和模型抖动。
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.openai_adapter import OpenAISemanticIntentLanguageModel

from corporate_travel_agent.demo import SHANGHAI_TZ
from corporate_travel_agent.services.evaluation_performance import load_model_price_table

REPO = Path(__file__).resolve().parents[1]
RUNNER_VERSION = "multicity-extraction-measure-v1"
CLOCK = datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI_TZ)

#: 判分项。名字就是报告里的列名。
ASPECTS = ("count", "route", "order", "windows", "stays")


@dataclass(frozen=True, slots=True)
class Case:
    """一条原话，外加它**正确**读出来是什么样。"""

    case_id: str
    title: str
    message: str
    #: 期望的航段序列 [(起, 讫), ...]，按走的顺序。城市用规范英文名，
    #: 模型给中文也算对——译名是别名表的事（第 04 步），不是抽取的事。
    expect_legs: tuple[tuple[str, str], ...]
    #: 每段期望的日期（出发日，ISO）。None 表示"这一段旅行者没说时间，不该被编出来"。
    expect_depart_dates: tuple[str | None, ...]
    #: 期望的过夜城市，按顺序。() 表示这趟不该有多处住宿。
    expect_stay_cities: tuple[str, ...] = ()
    notes: str = ""


#: 城市别名：模型给中文或英文都算对。**判的是"读没读对这座城市"，不是译名。**
ALIASES: dict[str, tuple[str, ...]] = {
    "Beijing": ("beijing", "北京", "北京市", "pek", "bjs"),
    "Shanghai": ("shanghai", "上海", "上海市", "sha", "pvg"),
    "Hangzhou": ("hangzhou", "杭州", "杭州市", "hgh"),
    "Guangzhou": ("guangzhou", "广州", "广州市", "can"),
    "Shenzhen": ("shenzhen", "深圳", "深圳市", "szx"),
    "Chengdu": ("chengdu", "成都", "成都市", "ctu"),
    "Xi'an": ("xi'an", "xian", "西安", "西安市", "sia", "xiy"),
    "Tokyo": ("tokyo", "东京", "東京", "tyo", "nrt", "hnd"),
    "Singapore": ("singapore", "新加坡", "sin"),
    "Nanjing": ("nanjing", "南京", "南京市", "nkg"),
}


CASES: tuple[Case, ...] = (
    Case(
        case_id="MC-01",
        title="三段，顺序与城市都在一句话里说清了",
        message=(
            "9月15号从北京去上海开会，9月18号去杭州见客户，9月19号回北京。"
            "上海和杭州各住两晚。"
        ),
        expect_legs=(("Beijing", "Shanghai"), ("Shanghai", "Hangzhou"), ("Hangzhou", "Beijing")),
        expect_depart_dates=("2026-09-15", "2026-09-18", "2026-09-19"),
        expect_stay_cities=("Shanghai", "Hangzhou"),
    ),
    Case(
        case_id="MC-02",
        title="四段，中间两段只说了先后没说日期",
        message=(
            "10月8号从北京出发去广州，谈完直接去深圳，再去成都，"
            "10月14号从成都回北京。"
        ),
        expect_legs=(
            ("Beijing", "Guangzhou"),
            ("Guangzhou", "Shenzhen"),
            ("Shenzhen", "Chengdu"),
            ("Chengdu", "Beijing"),
        ),
        # 中间两段旅行者没给日期——**不许编**。
        expect_depart_dates=("2026-10-08", None, None, "2026-10-14"),
        notes="考的是「没说的不许编」：中间两段没有日期，填出来就是错的",
    ),
    Case(
        case_id="MC-03",
        title="三段，说的顺序和走的顺序不一样；开会日 ≠ 出发日",
        message=(
            "月底要去趟西安见客户，不过得先从北京飞南京参加9月28号的会，"
            "西安那边是9月30号，10月2号回北京。"
        ),
        expect_legs=(("Beijing", "Nanjing"), ("Nanjing", "Xi'an"), ("Xi'an", "Beijing")),
        # 前两段**必须留空**：「参加9月28号的会」说的是一个**承诺**（要在某时出现在
        # 某地），不是出发日——从北京飞南京完全可能是前一晚走。把开会日当成出发日
        # 是替旅行者做决定。最后一段「10月2号回北京」才是真的说了出发日。
        #
        # 这条判据第一版我写错了：期望模型把开会日当出发日填进去。真跑一次
        # （`reports/evaluation-runs/multicity-extraction-120136/`）模型 3/3 拒绝这么读、
        # 并问了「北京飞南京的具体出发日期是哪天？」——**是判据错了，不是模型错了。**
        expect_depart_dates=(None, None, "2026-10-02"),
        notes=(
            "两件事一起考：legs 必须按**走的顺序**而不是说的顺序；"
            "开会日不许当成出发日，但明说了的那一段要填"
        ),
    ),
    Case(
        case_id="MC-04",
        title="英文，三段跨境",
        message=(
            "I need to fly from Beijing to Tokyo on Oct 12 for a client meeting, "
            "then on to Singapore on Oct 15, and home to Beijing on Oct 18. "
            "Two nights in Tokyo and three in Singapore."
        ),
        expect_legs=(("Beijing", "Tokyo"), ("Tokyo", "Singapore"), ("Singapore", "Beijing")),
        expect_depart_dates=("2026-10-12", "2026-10-15", "2026-10-18"),
        expect_stay_cities=("Tokyo", "Singapore"),
    ),
    Case(
        case_id="MC-05",
        title="**对照组：普通往返，legs 必须留空**",
        message="9月15号从北京去上海，9月16号中午前到，9月18号晚上回北京。",
        expect_legs=(),
        expect_depart_dates=(),
        notes="加了 legs 之后最容易出的退步：把一两段的行程也塞进 legs，两个视图从此对不上",
    ),
    Case(
        case_id="MC-06",
        title="三段，但其中一段旅行者明说了坐高铁",
        message=(
            "11月3号北京飞上海，11月5号坐高铁去杭州，11月6号从杭州飞回北京，"
            "杭州住一晚。"
        ),
        expect_legs=(("Beijing", "Shanghai"), ("Shanghai", "Hangzhou"), ("Hangzhou", "Beijing")),
        expect_depart_dates=("2026-11-03", "2026-11-05", "2026-11-06"),
        expect_stay_cities=("Hangzhou",),
        notes="交通方式是走法枚举的事（第 05 步 §31.2），这里只看航段有没有被读全",
    ),
)


def _canonical(value: object) -> str:
    text = str(value or "").strip().casefold()
    for canonical, aliases in ALIASES.items():
        if text == canonical.casefold() or text in aliases:
            return canonical
    return text


def _legs_of(intent: Any) -> list[tuple[str, str]]:
    return [
        (_canonical(leg.origin), _canonical(leg.destination))
        for leg in (getattr(intent, "legs", None) or [])
    ]


def _depart_dates_of(intent: Any) -> list[str | None]:
    dates: list[str | None] = []
    for leg in getattr(intent, "legs", None) or []:
        value = leg.depart_after
        dates.append(value.date().isoformat() if value is not None else None)
    return dates


def _score(case: Case, intent: Any) -> dict[str, bool]:
    """五项分别判。**一项不过不影响另一项**——要知道它错在哪，不是错没错。"""
    legs = _legs_of(intent)
    expected = list(case.expect_legs)

    count_ok = len(legs) == len(expected)
    # 路线：不看顺序，只看"该走的这几程都在、没有多出来的"。
    route_ok = Counter(legs) == Counter(expected)
    # 顺序：路线对了才谈得上顺序对不对。
    order_ok = route_ok and legs == expected
    # 时间窗：只对**旅行者说了日期**的那几段判，且没说的那几段必须是 null。
    if count_ok and len(case.expect_depart_dates) == len(legs):
        actual_dates = _depart_dates_of(intent)
        windows_ok = all(
            (actual == expect) if expect is not None else (actual is None)
            for actual, expect in zip(actual_dates, case.expect_depart_dates, strict=True)
        )
    else:
        windows_ok = not expected and not legs
    stays = [_canonical(item.city) for item in (getattr(intent, "stays", None) or [])]
    stays_ok = stays == list(case.expect_stay_cities)
    return {
        "count": count_ok,
        "route": route_ok,
        "order": order_ok,
        "windows": windows_ok,
        "stays": stays_ok,
    }


def _run_once(model: OpenAISemanticIntentLanguageModel, case: Case) -> dict[str, Any]:
    ledger = json.dumps(
        {"turns": [{"turn_index": 0, "role": "user", "content": case.message}]},
        ensure_ascii=False,
    )
    result = model.interpret_trip_intent(
        ledger,
        task_id=f"measure-{case.case_id}",
        traveler_id="E1001",
        context={
            "reference_time": CLOCK.isoformat(),
            "timezone": "Asia/Shanghai",
        },
    )
    intent = result.decision.intent
    scores = _score(case, intent)
    metadata = result.metadata
    return {
        "case_id": case.case_id,
        "status": result.decision.status.value,
        "scores": scores,
        "all_ok": all(scores.values()),
        "read_legs": _legs_of(intent),
        "read_depart_dates": _depart_dates_of(intent),
        "read_stays": [_canonical(item.city) for item in (intent.stays or [])],
        "clarification_question": result.decision.clarification_question,
        "usage": {
            "model": getattr(metadata, "model", None),
            "input_tokens": getattr(metadata, "input_tokens", None),
            "output_tokens": getattr(metadata, "output_tokens", None),
        },
    }


def _cost(usage: list[dict[str, Any]], prices: Any) -> float:
    total = 0.0
    for call in usage:
        price = prices.models.get(call.get("model") or "")
        if price is None:
            continue
        total += (call.get("input_tokens") or 0) * price.input / 1_000_000
        total += (call.get("output_tokens") or 0) * price.output / 1_000_000
    return total


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 多城航段抽取正确率 · 真实模型",
        "",
        f"- measured_at: `{payload['measured_at']}`",
        f"- runner: `{payload['runner_version']}`",
        f"- model: `{payload['model']}` · prompt `{payload['semantic_prompt_version']}`",
        f"- 每条跑 {payload['repeats']} 次，共 {payload['model_calls']} 次模型调用，"
        f"估算 **{payload['estimated_cost_usd']:.6f} {payload['currency']}**",
        "- **一次库存都没查**，不碰 Duffel / LiteAPI",
        "",
        "## 五项分开看",
        "",
        "| 项 | 通过 | 说明 |",
        "|---|---:|---|",
    ]
    meanings = {
        "count": "段数对不对",
        "route": "每段起讫对不对（不看顺序）",
        "order": "顺序对不对",
        "windows": "说了日期的段对不对、没说的段有没有被编出来",
        "stays": "过夜城市对不对",
    }
    total = payload["total_runs"]
    for aspect in ASPECTS:
        passed = payload["aspect_totals"][aspect]
        lines.append(f"| `{aspect}` | {passed}/{total} | {meanings[aspect]} |")
    lines.extend(
        [
            "",
            f"**五项全对：{payload['all_ok_runs']}/{total} 次**"
            f"（按用例算 {payload['cases_always_ok']}/{payload['case_count']} 条每次都全对）",
            "",
            "## 逐条",
            "",
            "| ID | 每次都全对 | 通过率 | Title |",
            "|---|---|---|---|",
        ]
    )
    for row in payload["cases"]:
        mark = "✅" if row["always_ok"] else "❌"
        lines.append(
            f"| {row['case_id']} | {mark} | {row['ok_runs']}/{row['runs']} | {row['title']} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--price-table", required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="只跑这几条（可重复）。改判据后单独复跑一条用，省钱",
    )
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.repeats <= 5:
        parser.error("--repeats must be between 1 and 5")
    selected = CASES
    if args.case_id:
        wanted = set(args.case_id)
        selected = tuple(case for case in CASES if case.case_id in wanted)
        if not selected:
            parser.error(f"没有匹配的用例：{sorted(wanted)}")
    calls = len(selected) * args.repeats
    if not args.confirm_billable_model_calls:
        parser.error(
            f"这一轮最多会真实调用 {calls} 次计费模型。确认后加 "
            "--confirm-billable-model-calls 重跑。"
        )
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not set")

    price_table, price_sha = load_model_price_table(args.price_table)
    if price_table.status != "configured" or args.model not in price_table.models:
        parser.error("the price table must be configured and contain the requested model")

    output = Path(args.output)
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)

    model = OpenAISemanticIntentLanguageModel(model=args.model)
    started = datetime.now(UTC).isoformat()
    runs: list[dict[str, Any]] = []
    for case in selected:
        for attempt in range(args.repeats):
            row = _run_once(model, case)
            row["attempt"] = attempt
            runs.append(row)

    usage = [row["usage"] for row in runs]
    aspect_totals = {
        aspect: sum(1 for row in runs if row["scores"][aspect]) for aspect in ASPECTS
    }
    cases_payload = []
    for case in selected:
        rows = [row for row in runs if row["case_id"] == case.case_id]
        ok = sum(1 for row in rows if row["all_ok"])
        cases_payload.append(
            {
                "case_id": case.case_id,
                "title": case.title,
                "notes": case.notes,
                "runs": len(rows),
                "ok_runs": ok,
                "always_ok": ok == len(rows),
                "attempts": rows,
            }
        )

    payload = {
        "measured_at": started,
        "runner_version": RUNNER_VERSION,
        "model": args.model,
        "semantic_prompt_version": model.semantic_prompt_version,
        "repeats": args.repeats,
        "case_count": len(selected),
        "total_runs": len(runs),
        "model_calls": len(runs),
        "aspect_totals": aspect_totals,
        "all_ok_runs": sum(1 for row in runs if row["all_ok"]),
        "cases_always_ok": sum(1 for row in cases_payload if row["always_ok"]),
        "price_table_version": price_table.price_table_version,
        "price_table_sha256": price_sha,
        "currency": price_table.currency,
        "estimated_cost_usd": _cost(usage, price_table),
        "cases": cases_payload,
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
