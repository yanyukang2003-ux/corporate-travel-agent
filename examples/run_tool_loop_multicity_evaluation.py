#!/usr/bin/env python3
"""多行程边角：把工具循环放在最容易连锁出错的地方，用真模型跑。

## 为什么是多城

一次性抽取里，一趟行程是一个结构体，错了在编译期还有一道拦。循环里每一段都是
一次独立的工具调用，**前一段读错会顺着往后传**。所以如果这个架构有问题，最先在
多城上显形。

覆盖的边角：按顺序的三城、**倒着叙述**的三城、开口程（中间一段自理）、
**完全没给日期**、只给了第一段时间、**说到一半改主意**、其中一段真的没货、
以及只在某一站住宿。

## 库存是显式路线表，不是 demo 那份

demo 目录只有北京↔上海，多城会全程搜空——那样量到的是"没货"，不是"读得对不对"。
这里的替身按一张**显式路线表**给货：表里有的路线随便哪天都给一条，表里没有的
（上海→成都）永远空。这样"这一段没货"是我故意设的，不是巧合。

## 花什么钱

只调语言模型（一条 case 好几轮）。库存全程走替身，不碰 Duffel / LiteAPI。

```bash
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/run_tool_loop_multicity_evaluation.py \\
  --output reports/evaluation-runs/toolloop-mc-NEWDIR \\
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
from zoneinfo import ZoneInfo

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

RUNNER_VERSION = "tool-loop-multicity-runner-v1"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CLOCK = datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI_TZ)

#: 有货的路线。**上海→成都故意不在表里**，MC-07 靠它测"一段没货怎么收场"。
ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("Beijing", "Shanghai"),
        ("Shanghai", "Hangzhou"),
        ("Hangzhou", "Beijing"),
        ("Shanghai", "Beijing"),
        ("Hangzhou", "Shanghai"),
        ("Beijing", "Hangzhou"),
        ("Chengdu", "Beijing"),
    }
)


class RouteTableProvider(MockProvider):
    """按显式路线表给货的替身：表里有就造一条，表里没有就永远空。"""

    def search_transport(self, query: TransportSearchQuery) -> Any:
        if (query.origin, query.destination) not in ROUTES:
            return self._snapshot("transport", query, ())
        arrive = query.arrive_before or (query.depart_after + timedelta(hours=4))
        depart = max(query.depart_after, arrive - timedelta(hours=2, minutes=30))
        offer = TransportOffer(
            ref_id=f"RT-{query.origin[:2].upper()}{query.destination[:2].upper()}-"
            f"{depart.strftime('%m%d%H%M')}",
            snapshot_id="route-table",
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
            ref_id=f"RT-HT-{query.city[:3].upper()}-{query.check_in.isoformat()}",
            snapshot_id="route-table",
            provider="mock",
            name=f"{query.city} Inn",
            city=query.city,
            check_in=query.check_in,
            check_out=query.check_out,
            nightly_price=Decimal("180"),
            commute_minutes=15,
        )
        return self._snapshot("hotel", query, (offer,))


@dataclass(frozen=True, slots=True)
class MultiCityCase:
    """一条多行程边角用例。

    `expect_routes` 按**出行顺序**给：断言的是模型实际搜了哪几段、顺序对不对，
    不是它在结构体里声明了什么。
    """

    case_id: str
    title: str
    message: str
    expect_terminal: str | None = None  # "propose_options" | "ask_traveler" | None=不限
    expect_routes: tuple[tuple[str, str], ...] = ()
    forbid_cities: tuple[str, ...] = ()
    expect_no_transport_search: bool = False
    expect_hotel_cities: tuple[str, ...] | None = None
    question_tokens: tuple[str, ...] = ()
    #: 期望"排出能定的部分 + 把没定的挂出来"，而不是整轮停住。
    expect_partial_delivery: bool = False
    #: 这些目的地**不许被搜**——日期还没定下来，搜就是拿编的日期去问 Provider。
    #: 和 `expect_partial_delivery` 分开：MC-07 的缺口段有日期、必须搜过才知道没货。
    forbid_searching: tuple[str, ...] = ()


CASES: tuple[MultiCityCase, ...] = (
    MultiCityCase(
        case_id="MC-01",
        title="三城按顺序：最基本的多行程",
        message="8月5号从北京去上海开会，8月6号去杭州见客户，8月7号回北京",
        expect_terminal="propose_options",
        expect_routes=(("Beijing", "Shanghai"), ("Shanghai", "Hangzhou"), ("Hangzhou", "Beijing")),
    ),
    MultiCityCase(
        case_id="MC-02",
        title="倒着叙述：说的顺序和走的顺序相反",
        message=(
            "8月7号从杭州飞回北京，在那之前8月6号从上海去杭州，"
            "最开始是8月5号从北京到上海"
        ),
        expect_terminal="propose_options",
        expect_routes=(("Beijing", "Shanghai"), ("Shanghai", "Hangzhou"), ("Hangzhou", "Beijing")),
    ),
    MultiCityCase(
        case_id="MC-03",
        title="开口程：中间那段自理，不许替他补上",
        message="8月5号从北京去上海，8月7号从杭州回北京，中间从上海到杭州我自己想办法",
        expect_terminal="propose_options",
        expect_routes=(("Beijing", "Shanghai"), ("Hangzhou", "Beijing")),
    ),
    MultiCityCase(
        case_id="MC-04",
        title="完全没给日期：不许编一个去搜",
        message="我要从北京去上海，再去杭州，然后回北京",
        expect_terminal="ask_traveler",
        expect_no_transport_search=True,
        question_tokens=("日期", "几号", "什么时候", "哪天"),
    ),
    MultiCityCase(
        case_id="MC-05",
        title="只给了第一段的时间：把能定的排出来，只问没定的",
        # 期望在 2026-08-29 改过：此前要求 ask_traveler（整轮停住）。
        # 同一句话交给 GPT，它把已经确定的那段排出来、只问缺的那段——那才是对的。
        # 现在 propose_options 可以带 open_questions，"交付一半"是合法终局。
        message="8月5号上午10点前要到上海，我从北京走，之后还要去杭州、再回北京",
        expect_routes=(("Beijing", "Shanghai"),),
        expect_partial_delivery=True,
        forbid_searching=("Hangzhou",),
        question_tokens=("杭州", "日期", "几号", "哪天", "出发"),
    ),
    MultiCityCase(
        case_id="MC-06",
        title="说到一半改主意：杭州那段作废",
        message=(
            "8月5号从北京去上海，然后去杭州……等等，杭州不去了，"
            "直接从上海回北京，8月6号回"
        ),
        expect_terminal="propose_options",
        expect_routes=(("Beijing", "Shanghai"), ("Shanghai", "Beijing")),
        forbid_cities=("Hangzhou",),
    ),
    MultiCityCase(
        case_id="MC-07",
        title="其中一段真的没货：有货的照排，没货的挂出来",
        # 期望在 2026-08-29 和 MC-05 一起改过：此前要求 ask_traveler（整轮停住）。
        # 缺口段和 MC-05 不同——它**有**日期，必须真搜过才知道没货，所以不设禁搜。
        message="8月5号从北京去上海，8月6号去成都，8月8号从成都回北京",
        expect_partial_delivery=True,
        question_tokens=("成都", "没有", "库存", "航班"),
    ),
    MultiCityCase(
        case_id="MC-08",
        title="只在某一站住：上海不住、杭州住一晚",
        message=(
            "8月5号北京去上海，8月6号去杭州，8月7号回北京。"
            "上海当天不过夜，杭州帮我订一晚"
        ),
        expect_terminal="propose_options",
        expect_routes=(("Beijing", "Shanghai"), ("Shanghai", "Hangzhou"), ("Hangzhou", "Beijing")),
        expect_hotel_cities=("Hangzhou",),
    ),
)


def _build_executor(conversation: str) -> ToolExecutor:
    return ToolExecutor(
        provider=RouteTableProvider([], [], now=CLOCK),
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
            hotel_city_caps={
                "Shanghai": Decimal("300"),
                "Hangzhou": Decimal("300"),
                "Beijing": Decimal("300"),
                "Chengdu": Decimal("300"),
            },
            arrival_buffer_minutes=0,
            exception_allowed_rule_ids=frozenset(),
            effective_from=date(2026, 1, 1),
        ),
        city_normalizer=CityNormalizer(load_policy_configuration().city_aliases),
        now=CLOCK,
        conversation=conversation,
    )


def _check(name: str, ok: bool, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)}


def _evaluate(
    case: MultiCityCase, outcome: LoopOutcome, executor: ToolExecutor
) -> list[dict[str, Any]]:
    # 只看**真的执行了**的搜索：被工具入口拒掉的调用不构成一段行程。
    searched = [(query.origin, query.destination) for query, _ in executor.leg_searches]
    attempted = [
        (
            str(ex.invocation.arguments.get("origin")),
            str(ex.invocation.arguments.get("destination")),
        )
        for ex in outcome.transcript
        if ex.invocation.name == "search_transport"
    ]
    stayed = [query.city for query, _ in executor.stay_searches]
    checks: list[dict[str, Any]] = []

    if case.expect_terminal is not None:
        checks.append(_check("terminal_action", outcome.kind == case.expect_terminal, outcome.kind))

    if case.expect_routes:
        checks.append(
            _check(
                "routes_in_travel_order",
                searched == list(case.expect_routes),
                f"expected {list(case.expect_routes)}, searched {searched}",
            )
        )

    if case.forbid_cities:
        # 用**尝试过的**调用来判：只要开口搜过就算没听懂，哪怕搜空了。
        touched = sorted(
            {city for pair in attempted for city in pair if city in case.forbid_cities}
        )
        checks.append(_check("dropped_city_stays_dropped", not touched, f"touched {touched}"))

    if case.expect_no_transport_search:
        # 红线是**有没有真的拿一个编出来的日期去搜库存**，不是模型有没有试过。
        # 被工具入口拒掉的调用正是边界在起作用，不该记成失败。
        checks.append(
            _check("no_inventory_search_without_dates", not searched, f"executed {searched}")
        )

    if case.expect_hotel_cities is not None:
        checks.append(
            _check(
                "hotels_only_where_asked",
                sorted(set(stayed)) == sorted(set(case.expect_hotel_cities)),
                f"expected {list(case.expect_hotel_cities)}, searched {stayed}",
            )
        )

    if case.expect_partial_delivery:
        # 已经能定的那段必须真的搜了——不许因为后面缺信息就把前面一起放弃。
        checks.append(
            _check("settled_legs_were_searched", bool(searched), f"searched {searched}")
        )
        # 没定的那段必须挂出来，不许悄悄跳过。
        pending = outcome.open_questions or ((outcome.question,) if outcome.question else ())
        checks.append(_check("undecided_leg_is_raised", bool(pending), f"pending {pending}"))

    if case.forbid_searching:
        # 日期还没定的那段绝不许被搜：搜了就是拿编的日期去问 Provider。
        touched = sorted(
            {city for pair in attempted for city in pair if city in case.forbid_searching}
        )
        checks.append(_check("undecided_leg_was_not_searched", not touched, f"touched {touched}"))

    if case.question_tokens and (outcome.kind == "ask_traveler" or outcome.open_questions):
        question = outcome.question or "\n".join(outcome.open_questions)
        checks.append(
            _check(
                "question_names_the_gap",
                any(token in question for token in case.question_tokens),
                f"expected one of {case.question_tokens} in {question!r}",
            )
        )
    return checks


def _run_case(
    case: MultiCityCase,
    *,
    model: OpenAIToolCallingLanguageModel,
    recorder: LiveCaseRecorder,
    max_iterations: int,
) -> dict[str, Any]:
    executor = _build_executor(case.message)
    runner = ToolLoopRunner(
        model=model, executor=executor, max_iterations=max_iterations, invoke=recorder.invoke
    )
    context = {"reference_time": CLOCK.isoformat(), "timezone": "Asia/Shanghai"}
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "title": case.title,
        "message": case.message,
    }
    try:
        outcome = runner.run(case.message, context=context)
    except ToolLoopAborted as exc:
        record["error"] = f"ToolLoopAborted: {exc}"
        record["transcript"] = [
            {"tool": ex.invocation.name, "arguments": dict(ex.invocation.arguments), "ok": ex.ok}
            for ex in exc.transcript
        ]
        record["checks"] = [_check("loop_converged", False, str(exc))]
        record["passed"] = False
    except LanguageModelError as exc:
        # 传输层故障（超时、连不上）和"模型想错了"是两回事。这个 runner 直接调模型，
        # 没有 orchestrator 那层有界重试，所以网络抖一下就会记成一条失败——标出来，
        # 别让读报告的人把它当成能力问题。分数照记不放水，但标签要在。
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["error_kind"] = (
            "transport" if getattr(exc, "layer", "").startswith("openai_") else "model"
        )
        record["checks"] = [_check("loop_converged", False, str(exc))]
        record["passed"] = False
    else:
        record["outcome_kind"] = outcome.kind
        record["question"] = outcome.question
        record["open_questions"] = list(outcome.open_questions)
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
        record["refused_calls"] = [
            {
                "tool": ex.invocation.name,
                "arguments": dict(ex.invocation.arguments),
                "error": str(ex.result.get("error", ""))[:200],
            }
            for ex in outcome.transcript
            if not ex.ok
        ]
        record["checks"] = _evaluate(case, outcome, executor)
        record["passed"] = all(check["ok"] for check in record["checks"])
    record["llm_calls"] = model.call_count
    record["input_tokens"] = model.input_tokens
    record["output_tokens"] = model.output_tokens
    return record


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
    # 默认 10 轮 = 宿主给 /agentic 的 20 次预算除以每轮两次。
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--case", action="append", help="只跑指定 case_id，可重复")
    parser.add_argument("--gate", action="store_true")
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
        dataset_id="tool-loop-multicity-cases",
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
            failed = [c["name"] for c in record["checks"] if not c["ok"]]
            print(
                f"[{status}] {case.case_id} attempt={attempt} "
                f"kind={record.get('outcome_kind', 'ABORTED')} "
                f"llm_calls={record['llm_calls']}"
                + (f" failed={failed}" if failed else "")
            )

    # 任一次重复失败就算这条没过——不许挑最好的那次。
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
        "reference_time": CLOCK.isoformat(),
        "routes_with_inventory": sorted(f"{o}->{d}" for o, d in ROUTES),
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
    return 1 if args.gate and passed != len(by_case) else 0


if __name__ == "__main__":
    raise SystemExit(main())
