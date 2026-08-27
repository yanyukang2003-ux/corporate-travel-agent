#!/usr/bin/env python3
"""HANDOFF §18.3 H 的 8 条日期边角，走语义入口 + 真实计费模型。

## 这个 runner 在补哪个洞

旧的 `run_calendar_edge_acceptance.py` 用脚本化替身验证**宿主**的日历机器算得对不对。
ADR-0002 把那套机器从语义链路里删掉了：日期解析现在归模型。所以在新链路上，这 8 条
用脚本化替身根本没法验——只能真跑计费模型。这是当前语义链路最大的测试空白。

## 这是测量，不是门禁

新链路在这 8 条上从没有过基线，所以默认不因失败退出非零码；加 `--gate` 才当门禁用。
失败本身就是有价值的结论。

## 花什么钱

只调语言模型。库存用 Mock，不碰 Duffel / LiteAPI，不产生任何 Provider 费用。
每条用例一次模型调用（模型层最多重试 1 次），共 8 条。

```bash
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/run_semantic_calendar_model_evaluation.py \\
  --output reports/evaluation-runs/semantic-calendar-model-NEWDIR \\
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \\
  --confirm-billable-model-calls
```

输出目录必须不存在。
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.openai_adapter import OpenAISemanticIntentLanguageModel
from corporate_travel_agent.demo import SHANGHAI_TZ, build_demo_system
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.evaluation_performance import load_model_price_table

REPO = Path(__file__).resolve().parents[1]
RUNNER_VERSION = "semantic-calendar-model-runner-v1"
GATE_ID = "H-semantic"

# 与 examples/run_calendar_edge_acceptance.py 冻结的参照时刻保持一致。
CLOCK = datetime(2026, 8, 19, 15, 0, tzinfo=SHANGHAI_TZ)
EARLY = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)


@dataclass(frozen=True, slots=True)
class CalendarCase:
    """一条日期边角用例；期望值从旧 H 验收原样搬来，不是本轮新写的答案。"""

    case_id: str
    title: str
    message: str
    clock: datetime
    expect_departure_date: str | None
    expect_return_date: str | None = None
    expect_departure_unresolved: bool = False
    expect_arrive_unresolved: bool = False
    expect_clarification: bool = False
    expect_cities: bool = False
    forbid_year_rollforward: bool = False
    expect_no_search: bool = False
    question_tokens: tuple[str, ...] = ()


CASES: tuple[CalendarCase, ...] = (
    CalendarCase(
        case_id="H-01",
        title="下下周三 = 下下个星期的星期三",
        message="下下周三从北京去上海开会",
        clock=CLOCK,
        expect_departure_date="2026-09-02",
    ),
    CalendarCase(
        case_id="H-02",
        title="这周五还是下周五：不许自己挑一个",
        message="这周五还是下周五从北京去上海",
        clock=CLOCK,
        expect_departure_date=None,
        expect_departure_unresolved=True,
        expect_arrive_unresolved=True,
        expect_clarification=True,
        question_tokens=("周五", "日期", "公历"),
    ),
    CalendarCase(
        case_id="H-03a",
        title="8/5 在该日之前 = 今年 8 月 5 日",
        message="8/5从北京去上海开会",
        clock=EARLY,
        expect_departure_date="2026-08-05",
    ),
    CalendarCase(
        case_id="H-03b",
        title="8.5 在该日之前 = 今年 8 月 5 日",
        message="8.5从北京去上海开会",
        clock=EARLY,
        expect_departure_date="2026-08-05",
    ),
    CalendarCase(
        case_id="H-03c",
        title="2026.8.5 写了年份就按年份，哪怕已经过去",
        message="2026.8.5从北京去上海开会",
        clock=CLOCK,
        expect_departure_date="2026-08-05",
    ),
    # 期望在 2026-08-27 按项目所有者定的规则重写过，不再等同于旧链路的行为。
    # 旧链路：整条日期留空不解析。
    # 新规则：没写年份就按今年算；这一天已经过去 → 要么问是不是明年，要么直接告诉
    # 用户日期已过。两种都算过，因为两者都满足真正的红线：绝不悄悄顺延到明年、
    # 绝不拿一个过去的日期去搜库存。
    CalendarCase(
        case_id="H-03d",
        title="没写年份的 8/5 已经过去：问哪一年或报日期已过，绝不顺延到明年",
        message="8/5从北京去上海开会",
        clock=CLOCK,
        expect_departure_date=None,
        forbid_year_rollforward=True,
        expect_clarification=True,
        expect_no_search=True,
        question_tokens=("明年", "哪一年", "日期已过"),
    ),
    CalendarCase(
        case_id="H-04",
        title="春节不许编成某个公历日",
        message="春节从北京去上海出差",
        clock=CLOCK,
        expect_departure_date=None,
        expect_departure_unresolved=True,
        expect_clarification=True,
        expect_cities=True,
    ),
    CalendarCase(
        case_id="H-05",
        title="12月30日去、1月2日回：返程跨到下一年",
        message="12月30日从北京去上海开会，1月2日回",
        clock=CLOCK,
        expect_departure_date="2026-12-30",
        expect_return_date="2027-01-02",
    ),
)


def _check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _date_of(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)[:10]


def _evaluate(case: CalendarCase, task: Any) -> list[dict[str, Any]]:
    fields = task.intent_fields
    depart = fields.get("departure_after")
    arrive = fields.get("arrive_by")
    ret = fields.get("return_after")
    checks: list[dict[str, Any]] = []

    if case.expect_departure_date is not None:
        checks.append(
            _check(
                "departure_date",
                _date_of(depart) == case.expect_departure_date,
                f"expected {case.expect_departure_date}, got {_date_of(depart)}",
            )
        )
    if case.expect_return_date is not None:
        checks.append(
            _check(
                "return_date",
                _date_of(ret) == case.expect_return_date,
                f"expected {case.expect_return_date}, got {_date_of(ret)}",
            )
        )
    if case.expect_departure_unresolved:
        checks.append(_check("departure_unresolved", depart is None, _date_of(depart)))
    if case.expect_arrive_unresolved:
        checks.append(_check("arrive_unresolved", arrive is None, _date_of(arrive)))
    if case.expect_clarification:
        checks.append(
            _check(
                "clarifies",
                task.state is TaskState.NEEDS_CLARIFICATION,
                task.state.value,
            )
        )
        checks.append(_check("no_options", not task.options, len(task.options)))
    if case.expect_cities:
        checks.append(_check("origin", fields.get("origin") == "Beijing", fields.get("origin")))
        checks.append(
            _check(
                "destination",
                fields.get("destination") == "Shanghai",
                fields.get("destination"),
            )
        )
    if case.question_tokens:
        question = task.clarification_question or ""
        checks.append(
            _check(
                "question_names_the_ambiguity",
                any(token in question for token in case.question_tokens),
                question[:200],
            )
        )
    if case.forbid_year_rollforward:
        # 没解析（问了年份）可以；解析了就必须还是参照年，绝不能顺延到下一年。
        rolled = isinstance(depart, datetime) and depart.year != case.clock.year
        checks.append(_check("not_rolled_forward", not rolled, _date_of(depart)))
    # 任何用例都不许在读不准时已经搜了库存。
    if case.expect_departure_unresolved or case.expect_no_search:
        searches = sum(
            1 for item in task.tool_calls if item.tool_name.startswith("provider.search")
        )
        checks.append(_check("no_search_on_unresolved", searches == 0, searches))
    return checks


def _usage(task: Any) -> list[dict[str, Any]]:
    return list(task.metadata.get("llm_calls") or [])


def _cost(usage: list[dict[str, Any]], prices: Any) -> float:
    total = 0.0
    for call in usage:
        price = prices.models.get(call.get("model") or "")
        if price is None:
            continue
        total += (call.get("input_tokens") or 0) * price.input / 1_000_000
        total += (call.get("output_tokens") or 0) * price.output / 1_000_000
    return total


def _run_case(case: CalendarCase, model_factory) -> dict[str, Any]:
    workflow, _ = build_demo_system(
        semantic_language_model=model_factory(),
        clock=lambda case=case: case.clock,
    )
    failure: str | None = None
    try:
        task = workflow.create_task_from_semantic_message(
            case.message, traveler_id="E1001", task_id=case.case_id.lower()
        )
    except Exception as exc:  # noqa: BLE001 - 记录下来，不让一条用例炸掉整轮
        return {
            "case_id": case.case_id,
            "title": case.title,
            "message": case.message,
            "reference_time": case.clock.isoformat(),
            "passed": False,
            "checks": [_check("model_call", False, f"{type(exc).__name__}: {exc}")],
            "state": None,
            "intent_fields": {},
            "clarification_question": None,
            "llm_calls": [],
        }

    checks = _evaluate(case, task)
    if task.state is TaskState.NEEDS_STRUCTURED_INPUT:
        failure = task.failure
        checks.append(_check("interpretation_succeeded", False, (failure or "")[:300]))
    return {
        "case_id": case.case_id,
        "title": case.title,
        "message": case.message,
        "reference_time": case.clock.isoformat(),
        "passed": all(item["ok"] for item in checks) if checks else False,
        "checks": checks,
        "state": task.state.value,
        "intent_fields": {
            key: str(value)
            for key, value in task.intent_fields.items()
            if value not in (None, [], "")
        },
        "clarification_question": task.clarification_question,
        "conflicts": list(task.intent_conflicts),
        "assumptions": list(task.assumptions),
        "failure": failure,
        "llm_calls": _usage(task),
    }


def _markdown(payload: dict[str, Any]) -> str:
    cases = payload["cases"]
    passed = payload["passed"]
    total = payload["total"]
    lines = [
        "# §18.3 H 日期边角 · 语义入口 · 真实模型",
        "",
        f"- started_at: `{payload['started_at']}`",
        f"- runner: `{payload['runner_version']}`",
        f"- model: `{payload['model']}`",
        "- entrypoint: `semantic`",
        "- inventory: `mock`（不碰 Duffel / LiteAPI）",
        f"- result: **{passed}/{total} PASS**（每条跑 {payload.get('repeats', 1)} 次，"
        f"全部通过才记 PASS）",
        f"- 估算费用: **{payload['estimated_cost_usd']:.6f} {payload['currency']}**"
        f"（价目表 `{payload['price_table_version']}`，缓存折扣未建模）",
        f"- 模型调用: {payload['model_calls_attempted']} 次，其中 "
        f"{payload['model_calls_without_usage']} 次返回了不合规 JSON。"
        f"**失败调用照样计费但拿不到 usage，不在台账里，所以上面的数字偏低。**",
        "",
        "**这是基线测量，不是门禁。** 语义链路在这 8 条上此前没有任何数据。",
        "期望值从 `examples/run_calendar_edge_acceptance.py` 原样搬来，不是本轮新写的答案。",
        "",
        "| ID | Result | 通过率 | Title |",
        "|---|---|---|---|",
    ]
    for item in cases:
        mark = "PASS" if item["passed"] else "FAIL"
        rate = f"{item.get('passes', int(item['passed']))}/{item.get('attempts', 1)}"
        lines.append(f"| {item['case_id']} | {mark} | {rate} | {item['title']} |")
    unstable = payload.get("unstable_cases") or []
    if unstable:
        lines.append("")
        lines.append(
            "**时好时坏（同一句话不同次结果不同）：** " + "、".join(unstable) + "。"
            "这类用例的单次结果是噪声，不能当作退步或修复的证据。"
        )
    lines.append("")
    failed = [item for item in cases if not item["passed"]]
    if failed:
        lines.append("## 没过的用例")
        lines.append("")
        for item in failed:
            lines.append(f"### {item['case_id']} — {item['title']}")
            lines.append("")
            lines.append(f"- 用户原话：`{item['message']}`")
            lines.append(f"- 参照时刻：`{item['reference_time']}`")
            lines.append(f"- 任务状态：`{item['state']}`")
            if item.get("clarification_question"):
                lines.append(f"- 追问：{item['clarification_question']}")
            for check in item["checks"]:
                if not check["ok"]:
                    lines.append(f"- **{check['name']}**：{check['detail']}")
            lines.append("")
    lines.append("## 全部用例的模型读数")
    lines.append("")
    lines.append("| ID | 状态 | departure_after | arrive_by | return_after |")
    lines.append("|---|---|---|---|---|")
    for item in cases:
        fields = item["intent_fields"]
        lines.append(
            f"| {item['case_id']} | {item['state']} | "
            f"{fields.get('departure_after', '—')} | "
            f"{fields.get('arrive_by', '—')} | "
            f"{fields.get('return_after', '—')} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--price-table", required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="每条用例跑几次。>1 时按通过率报告，用来区分真实退步和模型抖动。",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="当门禁用：有用例没过就退出非零码。默认只测量。",
    )
    args = parser.parse_args()

    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")
    if not args.confirm_billable_model_calls:
        parser.error(
            f"这一轮会真实调用 {len(CASES) * args.repeats} 次计费模型。确认后加 "
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

    def model_factory() -> OpenAISemanticIntentLanguageModel:
        return OpenAISemanticIntentLanguageModel(
            model=args.model,
            request_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "90")),
        )

    started = datetime.now(UTC).isoformat()
    # 模型对同一句话不是每次都给同样的读数（实测 "8/5" 和 "8.5" 会一个解析一个追问），
    # 所以单跑一次的分数是噪声。重复跑，用通过率代替"过没过"。
    cases = []
    for case in CASES:
        attempts = [_run_case(case, model_factory) for _ in range(args.repeats)]
        wins = sum(1 for item in attempts if item["passed"])
        record = dict(attempts[0])
        record["attempts"] = args.repeats
        record["passes"] = wins
        record["pass_rate"] = wins / args.repeats
        record["passed"] = wins == args.repeats
        record["stable"] = wins in {0, args.repeats}
        record["llm_calls"] = [call for item in attempts for call in item["llm_calls"]]
        record["all_attempts"] = [
            {
                "passed": item["passed"],
                "state": item["state"],
                "clarification_question": item["clarification_question"],
                "intent_fields": item["intent_fields"],
            }
            for item in attempts
        ]
        cases.append(record)
    passed = sum(1 for item in cases if item["passed"])
    all_usage = [call for item in cases for call in item["llm_calls"]]
    estimated = _cost(all_usage, price_table)
    # 校验失败的调用照样计费，但拿不到 usage，因此不在台账里。必须显式记下来，
    # 否则费用估算看起来比实际低。
    failed_calls = sum(1 for item in cases if not item["llm_calls"])

    payload = {
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "runner_version": RUNNER_VERSION,
        "gate_id": GATE_ID,
        "entrypoint": "semantic",
        "inventory": "mock",
        "model": args.model,
        "semantic_prompt_version": OpenAISemanticIntentLanguageModel.semantic_prompt_version,
        "model_calls_attempted": len(all_usage) + failed_calls,
        "model_calls_with_usage": len(all_usage),
        "model_calls_without_usage": failed_calls,
        "price_table_version": price_table.price_table_version,
        "price_table_sha256": price_sha,
        "currency": price_table.currency,
        "estimated_cost_usd": estimated,
        "measurement_only": not args.gate,
        "repeats": args.repeats,
        "unstable_cases": [
            item["case_id"] for item in cases if not item.get("stable", True)
        ],
        "passed": passed,
        "total": len(cases),
        "cases": cases,
    }
    (output / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    with (output / "cost-ledger.jsonl").open("w", encoding="utf-8") as handle:
        for item in cases:
            for call in item["llm_calls"]:
                handle.write(
                    json.dumps(
                        {"case_id": item["case_id"], **call}, ensure_ascii=False, default=str
                    )
                    + "\n"
                )
    (output / "REPORT.md").write_text(_markdown(payload), encoding="utf-8")

    print(f"{passed}/{len(cases)} PASS → {output}")
    print(
        f"model calls: {len(all_usage) + failed_calls} attempted, "
        f"{failed_calls} without usage  estimated: ${estimated:.6f}"
    )
    if args.gate and passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
