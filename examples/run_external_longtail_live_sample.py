#!/usr/bin/env python3
"""外部真话长尾集（D18）· 真模型抽样对照。

分层抽样（默认 ChinaTravel 20 / CrossWOZ 15 / AirDialogue 10 = 45 条，各来源取前 N 条，
确定性），真 DeepSeek + Duffel/LiteAPI 沙箱只读跑一遍，**同一份判据**（红线 + 弱真值，
`services/evaluation_external_longtail.py`）打分；同场用离线替身把同一批再跑一遍，
逐条并排——差异只能来自模型。接 §48 的记账层：traces.jsonl / cost-ledger / retry-cap。

```bash
set -a && . ./.env && set +a && export DATABASE_URL=
.venv/bin/python examples/run_external_longtail_live_sample.py \\
  --output reports/evaluation-runs/external-longtail-live-NEWDIR \\
  --price-table evals/pricing/model-prices-openai-20260802-v1.json \\
  --confirm-billable-model-calls --confirm-external-test-calls
```

不下单、不付钱。输出目录必须事先不存在。
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.tool_loop_adapter import (
    TOOL_LOOP_PROMPT_VERSION,
    OpenAIToolCallingLanguageModel,
)
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.evaluation.external_longtail import (
    case_record,
    load_cases,
    probe_cases,
)
from corporate_travel_agent.evaluation.performance import load_model_price_table
from corporate_travel_agent.evaluation.tool_loop import LiveRunLedger
from corporate_travel_agent.providers.factory import travel_provider_from_environment
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

RUNNER_VERSION = "external-longtail-live-sample-v1"


def _assert_live_sandbox() -> str:
    provider = os.getenv("TRAVEL_PROVIDER", "")
    if provider not in {"duffel", "duffel_liteapi"}:
        raise SystemExit(f"TRAVEL_PROVIDER 必须是真实沙箱（当前 {provider!r}）")
    if not os.getenv("DUFFEL_ACCESS_TOKEN", "").startswith("duffel_test_"):
        raise SystemExit("必须使用 duffel_test_ 令牌（只读沙箱）")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        raise SystemExit("LIVE_BOOKING_ENABLED 必须关闭")
    return provider


def _sample(cases: list[dict[str, Any]], quotas: dict[str, int]) -> list[dict[str, Any]]:
    """各来源取前 N 条：确定性，重复跑同一批。"""
    taken: dict[str, int] = {}
    picked = []
    for case in cases:
        source = case["source"]["dataset"]
        limit = quotas.get(source, 0)
        if taken.get(source, 0) < limit:
            taken[source] = taken.get(source, 0) + 1
            picked.append(case)
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("data/evaluation/external-longtail-v1")
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--price-table", type=Path)
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--chinatravel", type=int, default=20)
    parser.add_argument("--crosswoz", type=int, default=15)
    parser.add_argument("--airdialogue", type=int, default=10)
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    args = parser.parse_args()
    if not args.confirm_billable_model_calls:
        parser.error("会调用计费模型；请加 --confirm-billable-model-calls")
    if not args.confirm_external_test_calls:
        parser.error("会调用 Duffel/LiteAPI 只读搜索；请加 --confirm-external-test-calls")
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not set")
    if args.output.exists():
        parser.error(f"输出目录必须事先不存在，以免覆盖历史证据：{args.output}")
    provider_name = _assert_live_sandbox()

    cases, manifest = load_cases(args.dataset)
    sample = _sample(
        cases,
        {
            "LAMDA-NeSy/ChinaTravel": args.chinatravel,
            "thu-coai/CrossWOZ": args.crosswoz,
            "google/air_dialogue": args.airdialogue,
        },
    )
    policy = load_policy_configuration()
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
    normalizer = CityNormalizer(policy.city_aliases)

    price_table = price_sha = None
    if args.price_table:
        price_table, price_sha = load_model_price_table(args.price_table)
    ledger = LiveRunLedger(
        model=args.model,
        prompt_version=TOOL_LOOP_PROMPT_VERSION,
        runner_version=RUNNER_VERSION,
        dataset_id=manifest["dataset_id"],
        dataset_version=manifest["dataset_version"],
        dataset_sha256=manifest["cases_sha256"],
        price_table=price_table,
        price_table_sha256=price_sha,
    )

    # 同一批先过离线替身：并排对照的另一列。
    offline = {item["case_id"]: item for item in probe_cases(sample)}

    results: list[dict[str, Any]] = []
    started = datetime.now(UTC)
    try:
        for index, case in enumerate(sample):
            model = OpenAIToolCallingLanguageModel(
                model=args.model, request_timeout_seconds=90.0
            )
            recorder = ledger.open_case(case["case_id"], 1, model)
            workflow, _ = build_demo_system(
                tool_calling_language_model=model,
                provider=provider,
                trace_observer=recorder,
            )
            record: dict[str, Any] = {
                "case_id": case["case_id"],
                "language": case["language"],
                "source": case["source"]["dataset"],
            }
            try:
                task = workflow.create_task_from_agentic_message(
                    case["message"], traveler_id="E1001", task_id=f"xlt-live-{index:04d}"
                )
            except Exception as exc:  # noqa: BLE001 - 一条炸掉不该带走整轮
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["error_kind"] = (
                    "transport"
                    if str(getattr(exc, "layer", "")).startswith("openai_")
                    else "system"
                )
                record["passed"] = record["error_kind"] != "system"
                recorder.finish(state=None, failure_reason=record["error"])
            else:
                record = case_record(workflow, task, case, normalizer, base=record)
                recorder.finish(
                    state=task.state.value,
                    result_refs=tuple(
                        ref for option in task.options for ref in option.inventory_refs
                    ),
                    user_response=task.clarification_question,
                    failure_reason=task.failure,
                )
            record["llm_calls"] = model.call_count
            record["input_tokens"] = model.input_tokens
            record["output_tokens"] = model.output_tokens
            off = offline.get(case["case_id"], {})
            record["offline_state"] = off.get("state")
            record["state_agrees_with_offline"] = record.get("state") == off.get("state")
            results.append(record)
            mark = "PASS" if record.get("passed") else "FAIL"
            print(
                f"[{mark}] {record['case_id']} state={record.get('state')} "
                f"offline={record.get('offline_state')} searches={record.get('search_count', 0)} "
                f"llm={record['llm_calls']}"
            )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    states = Counter(str(item.get("state") or "ERROR") for item in results)
    offline_states = Counter(str(item.get("offline_state")) for item in results)
    summary = {
        "runner_version": RUNNER_VERSION,
        "prompt_version": TOOL_LOOP_PROMPT_VERSION,
        "model": args.model,
        "inventory": provider_name,
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["dataset_version"],
        "cases_sha256": manifest["cases_sha256"],
        "started_at": started.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "total": len(results),
        "passed": sum(1 for item in results if item.get("passed")),
        "with_searches": sum(1 for item in results if item.get("search_count")),
        "with_options": sum(1 for item in results if item.get("option_count")),
        "state_distribution": dict(states),
        "offline_state_distribution": dict(offline_states),
        "state_agreement": sum(1 for item in results if item.get("state_agrees_with_offline")),
        "llm_calls": sum(int(item.get("llm_calls") or 0) for item in results),
        "results": results,
    }
    args.output.mkdir(parents=True)
    summary["live_artifacts"] = ledger.write(args.output)
    (args.output / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(
        f"\n红线 {summary['passed']}/{summary['total']}；搜索了 {summary['with_searches']} 条；"
        f"真实终态 {json.dumps(summary['state_distribution'], ensure_ascii=False)}；"
        f"离线终态 {json.dumps(summary['offline_state_distribution'], ensure_ascii=False)}；"
        f"一致 {summary['state_agreement']}/{summary['total']}；"
        f"约 ${summary['live_artifacts']['estimated_cost_usd']}"
    )
    print(f"报告写入 {args.output / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
