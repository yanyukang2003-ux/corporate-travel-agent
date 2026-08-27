#!/usr/bin/env python3
"""D15：新语义意图入口的评测覆盖与新旧并排对比。

回答 ADR-0002 removal gate 的前两条：

1. 冻结用例通过**语义入口**逐条执行（D1 60 条工作流 + D2 480 条意图）；
2. 没有任何一条发生"静默错搜"——语义链路要么澄清、要么拒绝，绝不在含糊语义上调库存。

不调用计费模型，不访问外网。两条链路都用各自的确定性替身：旧链路用
``DeterministicChineseIntentParser``，新链路用 ``DeterministicSemanticInterpreter``；
两者的解析能力刻意对齐，因此观测差异归因于架构而不是解析器强弱。

输出目录必须不存在。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.agent.deterministic_semantic_interpreter import (
    SEMANTIC_PROMPT_VERSION,
    DeterministicSemanticInterpreter,
)
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.evaluation_dataset import (
    EvaluationDatasetError,
    load_evaluation_dataset,
    run_intent_evaluation,
    run_workflow_evaluation_case,
)

RUNNER_VERSION = "semantic-entrypoint-runner-v1"
GATE_ID = "D15"

# 语义链路上一律不可接受的安全违规。
SAFETY_GATES = (
    "premature_provider_call_rate",
    "inventory_hallucination_rate",
)

# 允许在没有完整语义时停下的状态；除此之外的非搜索终态都要单独解释。
_SAFE_PAUSE_STATES = frozenset(
    {
        TaskState.NEEDS_CLARIFICATION,
        TaskState.NEEDS_STRUCTURED_INPUT,
        TaskState.OUT_OF_SCOPE,
    }
)


def _metric_values(metrics: Any) -> dict[str, Any]:
    values = asdict(metrics)
    values.pop("observations")
    return values


def _run_workflow_cases(dataset: Any) -> dict[str, Any]:
    """D1 60 条工作流用例逐条走语义入口，并按冻结期望打硬断言。"""
    passed = 0
    failures: list[dict[str, Any]] = []
    silent_searches: list[str] = []
    for case in dataset.workflow_cases:
        observation = run_workflow_evaluation_case(
            case,
            semantic_language_model=DeterministicSemanticInterpreter(),
        )
        expected = case.expected
        mismatches: list[str] = []
        if observation.after_create_state is not expected.after_create_state:
            mismatches.append("after_create_state")
        if observation.after_selection_state is not expected.after_selection_state:
            mismatches.append("after_selection_state")
        if observation.selected_policy_outcome is not expected.selected_policy_outcome:
            mismatches.append("selected_policy_outcome")
        if observation.booking_intent_created is not expected.booking_intent_expected:
            mismatches.append("booking_intent_expected")
        if (
            expected.selected_inventory_ref is not None
            and expected.selected_inventory_ref not in observation.selected_inventory_refs
        ):
            mismatches.append("selected_inventory_ref")
        # 静默错搜：期望停下澄清，实际却已经把库存结果端到用户面前。
        if (
            expected.after_create_state in _SAFE_PAUSE_STATES
            and observation.task.options
        ):
            silent_searches.append(case.case_id)
        if mismatches:
            failures.append(
                {
                    "case_id": case.case_id,
                    "scenario": case.scenario,
                    "mismatches": mismatches,
                    "observed_state": observation.final_state.value,
                }
            )
        else:
            passed += 1
    return {
        "executed": len(dataset.workflow_cases),
        "passed": passed,
        "failures": failures,
        "silent_wrong_search_case_ids": silent_searches,
    }


def _compare(legacy: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
    """按指标做并排对比；不可比的指标显式标注而不是记 0。"""
    rows: dict[str, Any] = {}
    for key in legacy:
        if key in {"entrypoint", "classification_status"}:
            continue
        legacy_value = legacy[key]
        semantic_value = semantic.get(key)
        comparable = (
            key != "classification_accuracy"
            and isinstance(legacy_value, (int, float))
            and isinstance(semantic_value, (int, float))
        )
        rows[key] = {
            "legacy": legacy_value,
            "semantic": semantic_value,
            "delta": (semantic_value - legacy_value) if comparable else None,
            "status": "comparable" if comparable else "not_applicable",
        }
    rows["classification_accuracy"]["note"] = (
        "The semantic entrypoint has no scenario classifier by design (ADR-0002); "
        "only OUT_OF_SCOPE is comparable, via out_of_scope_accuracy."
    )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the frozen evaluation datasets through the semantic intent entrypoint."
    )
    parser.add_argument(
        "dataset_directory",
        nargs="?",
        default="data/evaluation/derived-v2",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--skip-legacy-baseline",
        action="store_true",
        help="Only run the semantic entrypoint; skip the side-by-side legacy baseline.",
    )
    args = parser.parse_args()

    output_root = Path(args.output)
    if output_root.exists():
        raise SystemExit(f"Output directory must not exist yet: {output_root}")

    dataset = load_evaluation_dataset(args.dataset_directory)

    workflow_results = _run_workflow_cases(dataset)

    semantic_metrics = run_intent_evaluation(dataset.intent_cases, entrypoint="semantic")
    semantic_values = _metric_values(semantic_metrics)
    legacy_values: dict[str, Any] | None = None
    if not args.skip_legacy_baseline:
        legacy_metrics = run_intent_evaluation(dataset.intent_cases, entrypoint="legacy")
        legacy_values = _metric_values(legacy_metrics)

    gate_failures: list[str] = []
    for gate in SAFETY_GATES:
        value = semantic_values.get(gate)
        if value is None or value > 0:
            gate_failures.append(f"intent.{gate}={value}")
    if workflow_results["silent_wrong_search_case_ids"]:
        gate_failures.append(
            "workflow.silent_wrong_search="
            + ",".join(workflow_results["silent_wrong_search_case_ids"])
        )
    if workflow_results["failures"]:
        gate_failures.append(
            f"workflow.hard_assertion_failures={len(workflow_results['failures'])}"
        )

    summary = {
        "schema_version": 1,
        "gate_id": GATE_ID,
        "runner_version": RUNNER_VERSION,
        "protocol_id": "agent-eval-v1",
        "evaluation_mode": "deterministic_mock",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_id": dataset.manifest.dataset_id,
        "dataset_version": dataset.manifest.dataset_version,
        "intent_entrypoint_under_test": "semantic",
        "semantic_prompt_version": SEMANTIC_PROMPT_VERSION,
        "workflow_cases": workflow_results,
        "intent_metrics_semantic": semantic_values,
        "intent_metrics_legacy": legacy_values,
        "side_by_side": (
            _compare(legacy_values, semantic_values) if legacy_values is not None else None
        ),
        "gate_passed": not gate_failures,
        "gate_failures": gate_failures,
        "limitations": [
            "All inventory is deterministic MOCK data.",
            "Both entrypoints run deterministic stand-ins, not a billed model.",
            "Tool choice is orchestrator-controlled, not open model tool selection.",
            "D1 case messages come from a frozen template, so intent parsing is easier "
            "than free-form production text.",
            "This run does not measure the semantic entrypoint under a real model.",
        ],
    }

    output_root.mkdir(parents=True)
    (output_root / "run-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_root / "evaluation-report.md").write_text(
        _render_report(summary),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if gate_failures:
        raise SystemExit(f"{GATE_ID} gate failed: {'; '.join(gate_failures)}")


def _render_report(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['gate_id']} 语义入口评测报告",
        "",
        f"- 运行器：`{summary['runner_version']}`",
        f"- 数据集：`{summary['dataset_id']}` v{summary['dataset_version']}",
        f"- 被测入口：`{summary['intent_entrypoint_under_test']}`",
        f"- 语义 prompt 版本：`{summary['semantic_prompt_version']}`",
        f"- 门禁：{'PASS' if summary['gate_passed'] else 'FAIL'}",
        "",
        "## D1 工作流用例（语义入口）",
        "",
        f"- 执行：{summary['workflow_cases']['executed']}",
        f"- 通过：{summary['workflow_cases']['passed']}",
        f"- 静默错搜：{len(summary['workflow_cases']['silent_wrong_search_case_ids'])}",
        "",
        "## D2 意图用例并排对比",
        "",
        "| 指标 | 旧链路 | 新语义链路 | 差值 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    rows = summary.get("side_by_side") or {}
    for key, row in rows.items():
        delta = "n/a" if row["delta"] is None else f"{row['delta']:+.4f}"
        lines.append(
            f"| `{key}` | {row['legacy']} | {row['semantic']} | {delta} | {row['status']} |"
        )
    lines += ["", "## 限制", ""]
    lines += [f"- {item}" for item in summary["limitations"]]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        main()
    except EvaluationDatasetError as exc:
        raise SystemExit(f"Evaluation dataset error: {exc}") from exc
