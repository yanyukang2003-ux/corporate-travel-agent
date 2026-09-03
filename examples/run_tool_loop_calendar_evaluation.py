#!/usr/bin/env python3
"""把第 0 步那 8 条日期边角原样跑一遍**工具循环**，和一次性抽取直接对照。

## 这个 runner 在回答哪个问题

第 0 步量的是旧架构（一次性抽取 + 一张全局必填表）在这 8 条上的分数：v13d 8/8。
`tests/test_tool_loop.py` 量的是循环代码本身对不对（模型是写死的剧本，不会犯错）。
**两边都没有回答"真模型在循环里能不能做对这 8 条"。** 这个 runner 只回答这一个问题。

## 变量隔离

- **同样 8 条 case、同样参照时刻、同样期望**：原先从第 0 步的
  `run_semantic_calendar_model_evaluation.py` 导入；那个 runner 随语义入口一起删了
  （ADR-0003，提交 9b816f1），用例和期望值按删除前的原文原样抄进本文件，
  不是本轮新写的答案。
- **同样的日期算术提示词段落**：第 0 步实测那几段承重，`tool_loop_adapter` 原样保留了。
  删掉的只有那张表的配套（输出信封、必填字段记账、READY 判据）。
- 于是唯一的差异是**架构**：字段表 vs 工具签名。

## 断言怎么从"字段"翻译成"工具调用"

旧链路看 `task.intent_fields["departure_after"]`；循环里没有那个字段，看的是**模型
实际拿什么日期去搜的**——`search_transport` 调用上的时间窗。这个观测比旧的更硬：
填对一个字段不等于会拿它去搜，搜对了才是真做对了。

## 库存用合成替身，不是 Mock 目录

这 8 条的日期散布在 9 月、12 月、次年 1 月，demo 的 Mock 目录只有 8 月 5 日那天有货。
库存空会让模型转去提问，把"日期算错"和"没搜到货"混成同一个失败。所以这里用一个
**日期无关**的合成 Provider：请求什么窗口就造一条落在窗口里的报价。本 runner 量的是
日期推理与工具选择，不是库存真实性。

## 花什么钱

只调语言模型，一条 case 会调好几轮（循环上限 12 轮）。不碰 Duffel / LiteAPI。

```bash
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/run_tool_loop_calendar_evaluation.py \\
  --output reports/evaluation-runs/toolloop-cal-NEWDIR \\
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \\
  --confirm-billable-model-calls
```

输出目录必须不存在。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.ports import LanguageModelError
from corporate_travel_agent.agent.tool_loop import (
    LoopOutcome,
    ToolExecutor,
    ToolLoopAborted,
    ToolLoopRunner,
)
from corporate_travel_agent.agent.tool_loop_adapter import (
    TOOL_LOOP_PROMPT_VERSION,
    OpenAIToolCallingLanguageModel,
)
from corporate_travel_agent.demo import SHANGHAI_TZ
from corporate_travel_agent.domain.enums import TransportMode
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    TransportOffer,
)
from corporate_travel_agent.evaluation.performance import load_model_price_table
from corporate_travel_agent.evaluation.tool_loop import (
    LiveCaseRecorder,
    LiveRunLedger,
)
from corporate_travel_agent.policy.engine import PolicyEngine
from corporate_travel_agent.providers.base import HotelSearchQuery, TransportSearchQuery
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

REPO = Path(__file__).resolve().parents[1]

RUNNER_VERSION = "tool-loop-calendar-runner-v1"

# 8 条 case、参照时刻与期望值：从第 0 步的 `run_semantic_calendar_model_evaluation.py`
# （删除前最后一版，`git show 9b816f1^:examples/run_semantic_calendar_model_evaluation.py`）
# 逐字抄来。期望值不许在本轮重写；改动期望要像 H-03d 那样把理由写在旁边。
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

#: 旧链路的 `expect_cities`（"城市要留着"）在循环里当场不可观测：还没搜之前，
#: 城市只存在于对话里，没有任何字段承载它。所以改成真的追问一轮，再看它搜的
#: 是哪两个城市——这才是那条断言原本想验的东西。
FOLLOW_UP: dict[str, str] = {
    "H-04": "就下个月10号出发吧，当天不回",
}


class SyntheticProvider(MockProvider):
    """日期无关的库存替身：请求什么窗口，就造一条落在窗口里的报价。

    **它不代表真实库存。** 它存在的唯一目的是让"日期算错"和"那天没货"不再混成
    同一个失败信号——本 runner 量的是日期推理，不是库存覆盖。
    """

    def search_transport(self, query: TransportSearchQuery) -> Any:
        arrive = query.arrive_before or (query.depart_after + timedelta(hours=3))
        depart = max(query.depart_after, arrive - timedelta(hours=2, minutes=15))
        offer = TransportOffer(
            ref_id=f"SYN-{query.origin[:2].upper()}{query.destination[:2].upper()}-"
            f"{depart.strftime('%m%d%H%M')}",
            snapshot_id="synthetic",
            provider="mock",
            mode=TransportMode.FLIGHT,
            origin=query.origin,
            destination=query.destination,
            depart_at=depart,
            arrive_at=arrive,
            price=Decimal("950"),
            seat_class="ECONOMY",
        )
        return self._snapshot("transport", query, (offer,))

    def search_hotels(self, query: HotelSearchQuery) -> Any:
        offer = HotelOffer(
            ref_id=f"SYN-HT-{query.city[:3].upper()}-{query.check_in.isoformat()}",
            snapshot_id="synthetic",
            provider="mock",
            name=f"{query.city} Inn",
            city=query.city,
            check_in=query.check_in,
            check_out=query.check_out,
            nightly_price=Decimal("180"),
            commute_minutes=15,
        )
        return self._snapshot("hotel", query, (offer,))


def _build_executor(clock: datetime, conversation: str = "") -> ToolExecutor:
    return ToolExecutor(
        provider=SyntheticProvider([], [], now=clock),
        policy_engine=PolicyEngine(),
        employee=EmployeeProfileSnapshot(
            snapshot_id="emp-1",
            employee_id="E-1",
            level="IC",
            department="Sales",
            home_city="Beijing",
            manager_id="M-1",
            profile_version=1,
        ),
        policy=PolicySnapshot(
            snapshot_id="pol-1",
            policy_version="v1",
            level_rules={"IC": LevelTravelRule(("ECONOMY",), ("SECOND",))},
            hotel_city_caps={"Shanghai": Decimal("300"), "Hangzhou": Decimal("300")},
            arrival_buffer_minutes=0,
            exception_allowed_rule_ids=frozenset(),
            effective_from=date(2026, 1, 1),
        ),
        city_normalizer=CityNormalizer(load_policy_configuration().city_aliases),
        now=clock,
        conversation=conversation,
    )


# ---------------------------------------------------------------------------
# 观测：从 transcript 里读出"模型实际拿什么日期去搜的"
# ---------------------------------------------------------------------------


def _searches(outcome: LoopOutcome, *, only_ok: bool = True) -> list[dict[str, Any]]:
    return [
        dict(ex.invocation.arguments)
        for ex in outcome.transcript
        if ex.invocation.name == "search_transport" and (ex.ok or not only_ok)
    ]


def _departure_dates(outcome: LoopOutcome, *, only_ok: bool = True) -> list[str]:
    """每次搜索的出发日：优先 `depart_after`，没填就落回 `arrive_by` 那天。

    这正是工具入口的推导规则，所以两者读出来是同一个日子。

    `only_ok=False` 把**被工具拒掉**的调用也算进来。这两个数必须分开看：
    模型读错日期，和模型读对了但工具按规则拒绝执行，是完全不同的失败，
    混在一起会把日期能力的分数记错。
    """
    out: list[str] = []
    for args in _searches(outcome, only_ok=only_ok):
        raw = args.get("depart_after") or args.get("arrive_by")
        if raw:
            out.append(str(raw)[:10])
    return out


def _check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _evaluate(case: CalendarCase, outcome: LoopOutcome) -> list[dict[str, Any]]:
    dates = _departure_dates(outcome)
    attempted = _departure_dates(outcome, only_ok=False)
    asked = outcome.kind == "ask_traveler"
    checks: list[dict[str, Any]] = []

    if case.expect_departure_date is not None:
        checks.append(
            _check(
                "departure_date_read_correctly",
                case.expect_departure_date in attempted,
                f"expected {case.expect_departure_date} among attempted searches, "
                f"got {attempted}",
            )
        )
        # 读对了还得真搜出去，才算这条链路走通。两者分开记：读对但被工具按
        # 规则拒掉（比如日期已过），是产品决策问题，不是模型能力问题。
        checks.append(
            _check(
                "departure_date_searched",
                case.expect_departure_date in dates,
                f"expected {case.expect_departure_date} among executed searches, got {dates}",
            )
        )
    if case.expect_return_date is not None:
        checks.append(
            _check(
                "return_date",
                case.expect_return_date in dates,
                f"expected {case.expect_return_date} among searches, got {dates}",
            )
        )
    if case.expect_departure_unresolved:
        # 日期没定就不许拿它去搜——循环里"没解析出来"的可观测形式就是"没搜"。
        checks.append(_check("no_search_on_unresolved_date", not dates, dates))
    if case.expect_clarification:
        checks.append(_check("clarifies", asked, outcome.kind))
    if case.expect_no_search:
        checks.append(_check("no_search", not dates, dates))
    if case.forbid_year_rollforward:
        checks.append(
            _check(
                "no_year_rollforward",
                not any(d.startswith("2027") for d in dates),
                dates,
            )
        )
    if case.question_tokens and asked:
        question = outcome.question or ""
        checks.append(
            _check(
                "question_mentions_ambiguity",
                any(token in question for token in case.question_tokens),
                f"expected one of {case.question_tokens} in {question!r}",
            )
        )
    return checks


# ---------------------------------------------------------------------------


def _run_case(
    case: CalendarCase,
    *,
    model: OpenAIToolCallingLanguageModel,
    recorder: LiveCaseRecorder,
    max_iterations: int,
) -> dict[str, Any]:
    executor = _build_executor(case.clock, case.message)
    runner = ToolLoopRunner(
        model=model, executor=executor, max_iterations=max_iterations, invoke=recorder.invoke
    )
    context = {
        "reference_time": case.clock.isoformat(),
        "timezone": "Asia/Shanghai",
    }
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "title": case.title,
        "message": case.message,
        "reference_time": case.clock.isoformat(),
    }
    try:
        outcome = runner.run(case.message, context=context)
    except (ToolLoopAborted, LanguageModelError) as exc:
        # 传输层故障（超时、连不上）和"模型想错了"是两回事：这个 runner 直接调模型，
        # 没有 orchestrator 那层有界重试。分数照记不放水，但标签要在。
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["error_kind"] = (
            "transport" if getattr(exc, "layer", "").startswith("openai_") else "model"
        )
        record["checks"] = [_check("loop_converged", False, str(exc))]
        record["passed"] = False
    else:
        record["outcome_kind"] = outcome.kind
        record["question"] = outcome.question
        record["summary"] = outcome.summary
        record["assumptions"] = list(executor.assumptions)
        record["transcript"] = [
            {
                "tool": ex.invocation.name,
                "arguments": dict(ex.invocation.arguments),
                "ok": ex.ok,
                "result": json.loads(json.dumps(dict(ex.result), default=str)),
            }
            for ex in outcome.transcript
        ]
        record["checks"] = _evaluate(case, outcome)
        follow_up = FOLLOW_UP.get(case.case_id)
        if follow_up and outcome.kind == "ask_traveler":
            record.update(_probe_second_turn(case, outcome, model, recorder, max_iterations))
            probe = record.get("follow_up_check")
            if probe is not None:
                record["checks"] = [*record["checks"], probe]
        record["passed"] = all(check["ok"] for check in record["checks"])
    record["llm_calls"] = model.call_count
    record["input_tokens"] = model.input_tokens
    record["output_tokens"] = model.output_tokens
    return record


def _probe_second_turn(
    case: CalendarCase,
    first: LoopOutcome,
    model: OpenAIToolCallingLanguageModel,
    recorder: LiveCaseRecorder,
    max_iterations: int,
) -> dict[str, Any]:
    """旅行者回答之后再跑一轮：城市有没有活下来，这时候才看得见。

    用和语义链路同一套 ledger 格式（`[turn:N role:X] ...`），别自己另造一种。
    """
    ledger = "\n".join(
        (
            f"[turn:0 role:user] {case.message}",
            f"[turn:1 role:assistant] {first.question}",
            f"[turn:2 role:user] {FOLLOW_UP[case.case_id]}",
        )
    )
    executor = _build_executor(case.clock, ledger)
    runner = ToolLoopRunner(
        model=model, executor=executor, max_iterations=max_iterations, invoke=recorder.invoke
    )
    context = {"reference_time": case.clock.isoformat(), "timezone": "Asia/Shanghai"}
    out: dict[str, Any] = {"follow_up": FOLLOW_UP[case.case_id]}
    try:
        second = runner.run(ledger, context=context)
    except (ToolLoopAborted, LanguageModelError) as exc:
        out["follow_up_error"] = f"{type(exc).__name__}: {exc}"
        out["follow_up_check"] = _check("cities_survive_a_clarification_round", False, str(exc))
        return out
    routes = {
        (str(args.get("origin")), str(args.get("destination")))
        for args in _searches(second, only_ok=False)
    }
    out["follow_up_kind"] = second.kind
    out["follow_up_question"] = second.question
    out["follow_up_routes"] = sorted(f"{o}->{d}" for o, d in routes)
    out["follow_up_check"] = _check(
        "cities_survive_a_clarification_round",
        any(city in origin for origin, _ in routes for city in ("北京", "Beijing"))
        and any(city in dest for _, dest in routes for city in ("上海", "Shanghai")),
        f"routes={sorted(routes)}",
    )
    return out


def _cases_sha256(cases) -> str:
    """用例本身的指纹：改了用例，轨迹指纹就变，旧报告不能冒充新的。"""
    payload = json.dumps(
        [asdict(case) for case in cases], ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finish_trace(recorder: LiveCaseRecorder, record: dict[str, Any]) -> None:
    """一条用例跑完，把终态写进它的轨迹；不论过没过、有没有炸。"""
    recorder.finish(
        state=record.get("state") or record.get("outcome_kind"),
        result_refs=tuple(str(ref) for ref in (record.get("option_refs") or ())),
        user_response=(
            record.get("user_visible_text") or record.get("question") or record.get("summary")
        ),
        failure_reason=record.get("error"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--price-table", type=Path)
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-iterations", type=int, default=12)
    parser.add_argument("--case", action="append", help="只跑指定 case_id，可重复")
    parser.add_argument("--gate", action="store_true", help="失败时返回非零码")
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    args = parser.parse_args(argv)

    if not args.confirm_billable_model_calls:
        parser.error("这个 runner 会调用计费模型；请加 --confirm-billable-model-calls")
    if args.output.exists():
        parser.error(f"输出目录必须事先不存在，以免覆盖历史证据：{args.output}")
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")

    selected = CASES
    if args.case:
        wanted = set(args.case)
        selected = tuple(case for case in CASES if case.case_id in wanted)
        if not selected:
            parser.error(f"没有匹配的 case_id：{sorted(wanted)}")

    price_table = None
    price_sha_for_ledger = None
    if args.price_table:
        price_table, price_sha_for_ledger = load_model_price_table(args.price_table)
    ledger = LiveRunLedger(
        model=args.model,
        prompt_version=TOOL_LOOP_PROMPT_VERSION,
        runner_version=RUNNER_VERSION,
        dataset_id="tool-loop-calendar-cases",
        dataset_version="1",
        dataset_sha256=_cases_sha256(selected),
        price_table=price_table,
        price_table_sha256=price_sha_for_ledger,
    )

    records: list[dict[str, Any]] = []
    for attempt in range(1, args.repeats + 1):
        for case in selected:
            model = OpenAIToolCallingLanguageModel(model=args.model)
            recorder = ledger.open_case(case.case_id, attempt, model)
            record = _run_case(
                case, model=model, recorder=recorder, max_iterations=args.max_iterations
            )
            _finish_trace(recorder, record)
            record["attempt"] = attempt
            records.append(record)
            status = "PASS" if record.get("passed") else "FAIL"
            print(
                f"[{status}] {case.case_id} attempt={attempt} "
                f"kind={record.get('outcome_kind', record.get('error', ''))} "
                f"llm_calls={record['llm_calls']}"
            )

    # 一条 case 在任一次重复里失败，就算这条没过——不许挑最好的那次。
    by_case: dict[str, bool] = {}
    for record in records:
        cid = str(record["case_id"])
        by_case[cid] = by_case.get(cid, True) and bool(record.get("passed"))
    passed = sum(1 for ok in by_case.values() if ok)

    input_tokens = sum(int(r["input_tokens"]) for r in records)
    output_tokens = sum(int(r["output_tokens"]) for r in records)
    llm_calls = sum(int(r["llm_calls"]) for r in records)
    cost: float | None = None
    price_table_version: str | None = None
    price_table_sha: str | None = None
    if args.price_table:
        price_table, price_table_sha = load_model_price_table(args.price_table)
        price_table_version = price_table.price_table_version
        entry = price_table.models.get(args.model)
        if entry is not None:
            # 缓存折扣未建模：多轮循环里 system 提示词是重复的，实际账单大概率更低。
            cost = (
                input_tokens * entry.input / 1_000_000
                + output_tokens * entry.output / 1_000_000
            )

    report = {
        "runner_version": RUNNER_VERSION,
        "prompt_version": TOOL_LOOP_PROMPT_VERSION,
        "architecture": "tool-loop",
        "generated_at": datetime.now(UTC).isoformat(),
        "model": args.model,
        "repeats": args.repeats,
        "max_iterations": args.max_iterations,
        "cases_total": len(by_case),
        "cases_passed": passed,
        "llm_calls": llm_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": cost,
        "price_table_version": price_table_version,
        "price_table_sha256": price_table_sha,
        "results": records,
    }
    args.output.mkdir(parents=True)
    report["live_artifacts"] = ledger.write(args.output)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    tail = f"，约 ${cost:.4f}（缓存折扣未建模）" if cost is not None else ""
    print(f"\n{passed}/{len(by_case)} 条通过；模型调用 {llm_calls} 次{tail}")
    print(f"报告写入 {args.output / 'report.json'}")
    if args.gate and passed != len(by_case):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
