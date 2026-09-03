#!/usr/bin/env python3
"""外部真话长尾集（D18）· 多轮续问：把"能不能办成"测出来。

单轮探针只量"不做坏事"。这里给每条用例配一张事实表（出发日、返程日、路线，来自
来源弱真值 + 固定默认值），系统追问时由脚本化的模拟旅行者逐轮交出去，最多三轮；
终态分成办成 / 诚实失败 / 仍在追问 / 降级 / 越界，红线判据照旧，另加"搜的是不是
事实表那天"。同时产出盲评输入 ``judge-inputs.jsonl``（对话原文 + 系统回复 + 方案投影），
给 ``run_output_quality_judge.py`` 用。

子集：ChinaTravel 154（其中苏州 35 条两家供应商都没映射，只量诚实失败）+
AirDialogue 订票 31 = 185 条。CrossWOZ 与取消/改签不在"办成"的定义里。

```bash
# $0 离线替身（CI 抽样也走这条）
.venv/bin/python examples/run_external_longtail_live_multiturn.py \\
  --offline --output reports/evaluation-runs/external-longtail-multiturn-offline-NEWDIR

# 真模型 + 真沙箱只读
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/run_external_longtail_live_multiturn.py \\
  --output reports/evaluation-runs/external-longtail-multiturn-live-NEWDIR \\
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \\
  --confirm-billable-model-calls --confirm-external-test-calls
```

不下单、不付钱。输出目录必须事先不存在。
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.ports import LanguageModelError
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.evaluation.external_longtail import (
    MAX_FOLLOW_UPS,
    MULTITURN_RUNNER_VERSION,
    FactSheet,
    build_fact_sheet,
    follow_up_message,
    judge_input_for_task,
    known_provider_places,
    load_cases,
    multiturn_record,
    render_multiturn_markdown,
    select_multiturn_cases,
    summarize_multiturn,
    turn_snapshot,
)
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

_SOURCE_FLAGS = {
    "chinatravel": "LAMDA-NeSy/ChinaTravel",
    "airdialogue": "google/air_dialogue",
}


def _assert_live_sandbox() -> str:
    provider = os.getenv("TRAVEL_PROVIDER", "")
    if provider not in {"duffel", "duffel_liteapi"}:
        raise SystemExit(f"TRAVEL_PROVIDER 必须是真实沙箱（当前 {provider!r}）")
    if not os.getenv("DUFFEL_ACCESS_TOKEN", "").startswith("duffel_test_"):
        raise SystemExit("必须使用 duffel_test_ 令牌（只读沙箱）")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        raise SystemExit("LIVE_BOOKING_ENABLED 必须关闭")
    return provider


def run_case(
    workflow: Any,
    case: dict[str, Any],
    fact: FactSheet,
    *,
    index: int,
    max_follow_ups: int,
    task_prefix: str,
) -> tuple[Any | None, list[dict[str, Any]], dict[str, Any]]:
    """跑一条：首句创建，追问就按事实表续答，最多 ``max_follow_ups`` 次。"""
    turns: list[dict[str, Any]] = []
    record: dict[str, Any] = {
        "case_id": case["case_id"],
        "language": case["language"],
        "source": case["source"]["dataset"],
    }
    task = None
    try:
        task = workflow.create_task_from_agentic_message(
            case["message"], traveler_id="E1001", task_id=f"{task_prefix}-{index:04d}"
        )
        turns.append(turn_snapshot(0, case["message"], task))
        for round_index in range(max_follow_ups):
            if task.state is not TaskState.NEEDS_CLARIFICATION:
                break
            said = follow_up_message(fact, round_index)
            task = workflow.submit_agentic_message(task.task_id, said)
            turns.append(turn_snapshot(round_index + 1, said, task))
    except LanguageModelError as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["error_kind"] = (
            "transport" if str(getattr(exc, "layer", "")).startswith("openai_") else "model"
        )
        record["passed"] = record["error_kind"] != "model"
        return None, turns, record
    except Exception as exc:  # noqa: BLE001 - 一条炸掉不该带走整轮
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["error_kind"] = "system"
        record["passed"] = False
        return None, turns, record
    return task, turns, record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("data/evaluation/external-longtail-v1")
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--offline", action="store_true", help="确定性替身 + Mock，$0")
    parser.add_argument("--price-table", type=Path)
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--max-follow-ups", type=int, default=MAX_FOLLOW_UPS)
    parser.add_argument(
        "--reference-date",
        type=date.fromisoformat,
        help="事实表的参照日（默认：离线用演示时钟那天，真跑用今天 UTC）",
    )
    parser.add_argument(
        "--sources",
        default="chinatravel,airdialogue",
        help="逗号分隔：chinatravel / airdialogue",
    )
    parser.add_argument("--limit-per-source", type=int, help="每来源只取前 N 条（冒烟用）")
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    args = parser.parse_args(argv)

    if args.output.exists():
        parser.error(f"输出目录必须事先不存在，以免覆盖历史证据：{args.output}")
    if not 1 <= args.max_follow_ups <= 5:
        parser.error("--max-follow-ups 取 1–5")
    wanted = {
        _SOURCE_FLAGS[item.strip()]
        for item in args.sources.split(",")
        if item.strip() in _SOURCE_FLAGS
    }
    if not wanted:
        parser.error("--sources 至少要有 chinatravel 或 airdialogue")

    cases, manifest = load_cases(args.dataset)
    subset = [case for case in select_multiturn_cases(cases) if case["source"]["dataset"] in wanted]
    if args.limit_per_source:
        taken: dict[str, int] = {}
        limited = []
        for case in subset:
            source = case["source"]["dataset"]
            if taken.get(source, 0) < args.limit_per_source:
                taken[source] = taken.get(source, 0) + 1
                limited.append(case)
        subset = limited

    policy = load_policy_configuration()
    normalizer = CityNormalizer(policy.city_aliases)
    known = known_provider_places()

    ledger = None
    price_table = price_sha = None
    if args.offline:
        from corporate_travel_agent.agent.deterministic_tool_model import (
            DeterministicToolCallingModel,
        )

        reference_date = args.reference_date or DEMO_CLOCK.date()
        mode = "offline_deterministic_standin"
        inventory_source = "MOCK"
        provider = None

        def make_model() -> Any:
            return DeterministicToolCallingModel()

        def make_workflow(model: Any, recorder: Any) -> Any:
            workflow, _ = build_demo_system(
                tool_calling_language_model=model, clock=lambda: DEMO_CLOCK
            )
            return workflow

    else:
        if not args.confirm_billable_model_calls:
            parser.error("会调用计费模型；请加 --confirm-billable-model-calls")
        if not args.confirm_external_test_calls:
            parser.error("会调用 Duffel/LiteAPI 只读搜索；请加 --confirm-external-test-calls")
        if not os.getenv("OPENAI_API_KEY"):
            parser.error("OPENAI_API_KEY is not set")
        from corporate_travel_agent.agent.tool_loop_adapter import (
            TOOL_LOOP_PROMPT_VERSION,
            OpenAIToolCallingLanguageModel,
        )
        from corporate_travel_agent.evaluation.performance import (
            load_model_price_table,
        )
        from corporate_travel_agent.evaluation.tool_loop import LiveRunLedger
        from corporate_travel_agent.providers.factory import travel_provider_from_environment

        provider_name = _assert_live_sandbox()
        active = next(
            (
                item
                for item in policy.policy_snapshots
                if item.snapshot_id == policy.config.active_policy_snapshot_id
            ),
            policy.policy_snapshots[0],
        )
        provider = travel_provider_from_environment(policy_currency=active.currency)
        if provider is None:
            raise SystemExit("未能从环境装出真实 Provider")
        reference_date = args.reference_date or datetime.now(UTC).date()
        mode = "model_live_provider"
        inventory_source = provider_name.upper() + "_SANDBOX"
        if args.price_table:
            price_table, price_sha = load_model_price_table(args.price_table)
        ledger = LiveRunLedger(
            model=args.model,
            prompt_version=TOOL_LOOP_PROMPT_VERSION,
            runner_version=MULTITURN_RUNNER_VERSION,
            dataset_id=manifest["dataset_id"],
            dataset_version=manifest["dataset_version"],
            dataset_sha256=manifest["cases_sha256"],
            price_table=price_table,
            price_table_sha256=price_sha,
        )

        def make_model() -> Any:
            return OpenAIToolCallingLanguageModel(model=args.model, request_timeout_seconds=90.0)

        def make_workflow(model: Any, recorder: Any) -> Any:
            workflow, _ = build_demo_system(
                tool_calling_language_model=model, provider=provider, trace_observer=recorder
            )
            return workflow

    facts = {
        case["case_id"]: build_fact_sheet(
            case, reference_date=reference_date, normalizer=normalizer, known=known
        )
        for case in subset
    }

    results: list[dict[str, Any]] = []
    judge_inputs: list[Any] = []
    started = datetime.now(UTC)
    try:
        for index, case in enumerate(subset):
            fact = facts[case["case_id"]]
            model = make_model()
            recorder = ledger.open_case(case["case_id"], 1, model) if ledger else None
            workflow = make_workflow(model, recorder)
            task, turns, record = run_case(
                workflow,
                case,
                fact,
                index=index,
                max_follow_ups=args.max_follow_ups,
                task_prefix="xlt-mt",
            )
            if task is not None:
                record = multiturn_record(
                    workflow, task, case, fact, normalizer, turns=turns, base=record
                )
                run_id = recorder.run_id if recorder else f"offline-{case['case_id']}"
                judge_inputs.append(
                    judge_input_for_task(
                        task, case, fact, run_id=run_id, inventory_source=inventory_source
                    )
                )
            else:
                record["turns"] = turns
                record["fact_sheet"] = fact.as_dict()
                record["expected_completable"] = fact.completable
                record["outcome"] = "crashed"
            record["llm_calls"] = getattr(model, "call_count", 0)
            record["input_tokens"] = getattr(model, "input_tokens", 0)
            record["output_tokens"] = getattr(model, "output_tokens", 0)
            if recorder is not None:
                recorder.finish(
                    state=task.state.value if task is not None else None,
                    result_refs=tuple(
                        ref
                        for option in (task.options if task else ())
                        for ref in option.inventory_refs
                    ),
                    user_response=task.clarification_question if task else None,
                    failure_reason=(task.failure if task else None) or record.get("error"),
                )
            results.append(record)
            mark = "PASS" if record.get("passed") else "FAIL"
            print(
                f"[{mark}] {record['case_id']} outcome={record.get('outcome')} "
                f"state={record.get('state')} follow_ups={record.get('follow_ups_used')} "
                f"searches={record.get('search_count', 0)} options={record.get('option_count', 0)} "
                f"llm={record['llm_calls']}",
                flush=True,
            )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    summary = summarize_multiturn(
        results,
        manifest,
        mode=mode,
        reference_date=reference_date,
        max_follow_ups=args.max_follow_ups,
    )
    summary["started_at"] = started.isoformat()
    if not args.offline:
        summary["model"] = args.model
        summary["prompt_version"] = ledger.prompt_version if ledger else None
    args.output.mkdir(parents=True)
    if ledger is not None:
        summary["live_artifacts"] = ledger.write(args.output)
    (args.output / "judge-inputs.jsonl").write_text(
        "".join(f"{item.model_dump_json()}\n" for item in judge_inputs), encoding="utf-8"
    )
    # 红线未过的用例交给 Judge 时要带 hard_rule_failed 标记（run_output_quality_judge 从这里读）。
    (args.output / "evaluation-result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "external-longtail-multiturn red lines",
                "hard_failures": [
                    {"case_id": item["case_id"], "checks": item.get("checks")}
                    for item in results
                    if not item.get("passed")
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (args.output / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "REPORT.md").write_text(render_multiturn_markdown(summary), encoding="utf-8")
    print(
        f"完成 {summary['completed']}/{summary['completable']}"
        f"（完成率 {summary['completion_rate']}），"
        f"红线 {summary['red_line_passed']}/{summary['total']}，报告 {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
