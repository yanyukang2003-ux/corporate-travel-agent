#!/usr/bin/env python3
"""D16：冻结评测集经**产品入口**（工具循环，`/agentic/trip-tasks`）执行，并与语义入口并排。

回答 ADR-0002 removal gate 在产品入口上的前两条：

1. 冻结用例通过**产品入口**逐条执行（D1 60 条工作流 + D2 480 条意图）；
2. 没有任何一条发生"静默错搜"——产品入口要么澄清、要么拒绝，绝不在含糊语义上调库存。

不调用计费模型，不访问外网。两条入口都用确定性替身，而且**共用同一个解释器**
（`DeterministicSemanticInterpreter`）：语义入口一次性编译，产品入口把编译结果翻成
工具调用。观测到的差异因此只能来自架构，不来自解析器强弱。

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
)
from corporate_travel_agent.agent.deterministic_tool_model import (
    TOOL_LOOP_STANDIN_VERSION,
    DeterministicToolCallingModel,
)
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.evaluation.dataset import (
    load_evaluation_dataset,
    run_intent_evaluation,
    run_workflow_evaluation_case,
)

RUNNER_VERSION = "product-entrypoint-runner-v2"
GATE_ID = "D16"

#: 语义入口删除前的最后一次并排报告（ADR-0003）。它的语义列现在是冻结基线：
#: 两个替身当时共用一个解释器，所以这一列仍然是"同样的理解、不同的架构"能给出的数。
SEMANTIC_BASELINE_REPORT = Path(
    "reports/evaluation-runs/product-entrypoint-20260901/run-summary.json"
)

#: 产品入口上一律不可接受的安全违规。
SAFETY_GATES = ("premature_provider_call_rate", "inventory_hallucination_rate")

#: 允许在没有完整语义时停下的状态；除此之外的非搜索终态都要单独解释。
_SAFE_PAUSE_STATES = frozenset(
    {TaskState.NEEDS_CLARIFICATION, TaskState.NEEDS_STRUCTURED_INPUT, TaskState.OUT_OF_SCOPE}
)

#: 没有暴露面、因而不做差值比较的指标，以及为什么。
_NOT_COMPARABLE: dict[str, str] = {
    "classification_accuracy": "两条入口都没有旧的场景分类器（ADR-0002）。",
    "missing_field_exact_match_rate": "工具循环没有必填表，一句追问背后没有字段名可读。",
    "missing_field_precision": "同上。",
    "missing_field_recall": "同上。",
    "out_of_scope_accuracy": "工具循环不产出分类标签；越界与否看 final_state。",
    "transport_preference_accuracy": (
        "工具循环里偏好只在交付时声明；停在追问的用例读不到，分母不同。"
    ),
    "scored_transport_preference_cases": "分母不同，见上一条。",
}


def _metric_values(metrics: Any) -> dict[str, Any]:
    values = asdict(metrics)
    values.pop("observations")
    return values


def _run_workflow_cases(dataset: Any) -> dict[str, Any]:
    """D1 60 条工作流用例逐条走产品入口，并按冻结期望打硬断言。"""
    passed = 0
    failures: list[dict[str, Any]] = []
    silent_searches: list[str] = []
    for case in dataset.workflow_cases:
        observation = run_workflow_evaluation_case(
            case, tool_calling_language_model=DeterministicToolCallingModel()
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
        if expected.after_create_state in _SAFE_PAUSE_STATES and observation.task.options:
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


def _compare(semantic: dict[str, Any], agentic: dict[str, Any]) -> dict[str, Any]:
    """按指标做并排对比；不可比的指标显式标注而不是记 0。"""
    rows: dict[str, Any] = {}
    for key, semantic_value in semantic.items():
        if key.endswith("_status") or key == "entrypoint":
            continue
        agentic_value = agentic.get(key)
        comparable = (
            key not in _NOT_COMPARABLE
            and isinstance(semantic_value, (int, float))
            and isinstance(agentic_value, (int, float))
        )
        row: dict[str, Any] = {
            "semantic": semantic_value,
            "agentic": agentic_value,
            "delta": (agentic_value - semantic_value) if comparable else None,
            "status": "comparable" if comparable else "not_applicable",
        }
        if key in _NOT_COMPARABLE:
            row["note"] = _NOT_COMPARABLE[key]
        rows[key] = row
    return rows


def _state_distribution(metrics: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in metrics.observations:
        counts[item.final_state] = counts.get(item.final_state, 0) + 1
    return dict(sorted(counts.items()))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the frozen evaluation datasets through the product (tool-loop) entrypoint."
    )
    parser.add_argument("dataset_directory", nargs="?", default="data/evaluation/derived-v2")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--skip-semantic-baseline",
        action="store_true",
        help="Skip the side-by-side against the frozen semantic baseline report.",
    )
    args = parser.parse_args()

    output_root = Path(args.output)
    if output_root.exists():
        raise SystemExit(f"Output directory must not exist yet: {output_root}")

    dataset = load_evaluation_dataset(args.dataset_directory)
    workflow_results = _run_workflow_cases(dataset)

    agentic_metrics = run_intent_evaluation(dataset.intent_cases, entrypoint="agentic")
    agentic_values = _metric_values(agentic_metrics)
    semantic_values: dict[str, Any] | None = None
    semantic_states: dict[str, int] | None = None
    if not args.skip_semantic_baseline:
        baseline = json.loads(SEMANTIC_BASELINE_REPORT.read_text(encoding="utf-8"))
        semantic_values = baseline["intent_metrics_semantic"]
        semantic_states = baseline.get("intent_final_states_semantic")

    gate_failures: list[str] = []
    for gate in SAFETY_GATES:
        value = agentic_values.get(gate)
        if value is None or value > 0:
            gate_failures.append(f"intent.{gate}={value}")
    if workflow_results["silent_wrong_search_case_ids"]:
        gate_failures.append(
            "workflow.silent_wrong_search="
            + ",".join(workflow_results["silent_wrong_search_case_ids"])
        )
    if workflow_results["failures"]:
        gate_failures.append(f"workflow.hard_assertion_failures={len(workflow_results['failures'])}")
    if semantic_values is not None and (
        agentic_values["clarification_accuracy"] < semantic_values["clarification_accuracy"]
    ):
        gate_failures.append(
            "intent.clarification_accuracy regressed against the semantic baseline: "
            f"{agentic_values['clarification_accuracy']} < "
            f"{semantic_values['clarification_accuracy']}"
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
        "intent_entrypoint_under_test": "agentic",
        "tool_loop_standin_version": TOOL_LOOP_STANDIN_VERSION,
        "semantic_prompt_version": SEMANTIC_PROMPT_VERSION,
        "workflow_cases": workflow_results,
        "intent_metrics_agentic": agentic_values,
        "intent_final_states_agentic": _state_distribution(agentic_metrics),
        "intent_metrics_semantic": semantic_values,
        "intent_final_states_semantic": semantic_states,
        "semantic_baseline_source": (
            str(SEMANTIC_BASELINE_REPORT) if semantic_values is not None else None
        ),
        "side_by_side": (
            _compare(semantic_values, agentic_values) if semantic_values is not None else None
        ),
        "gate_passed": not gate_failures,
        "gate_failures": gate_failures,
        "limitations": [
            "All inventory is deterministic MOCK data.",
            "The product entrypoint runs a deterministic stand-in; the semantic column is "
            "frozen from the last side-by-side before that entrypoint was deleted "
            "(ADR-0003). Both stand-ins shared one interpreter, so the comparison still "
            "measures architecture, not a model.",
            "The stand-in never retries or widens a search; a real model may.",
            "D1 case messages come from a frozen template, so intent parsing is easier "
            "than free-form production text.",
            "This run does not measure the product entrypoint under a real model; "
            "see the tool-loop live runs under reports/evaluation-runs/toolloop-*.",
        ],
    }

    output_root.mkdir(parents=True)
    (output_root / "run-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output_root / "evaluation-report.md").write_text(_render_report(summary), encoding="utf-8")
    printable = {key: value for key, value in summary.items() if key != "side_by_side"}
    print(json.dumps(printable, indent=2, ensure_ascii=False))
    if gate_failures:
        raise SystemExit(f"{GATE_ID} gate failed: {'; '.join(gate_failures)}")


def _render_report(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['gate_id']} 产品入口评测报告",
        "",
        "产品入口（`/agentic/trip-tasks`，工具循环）是员工和前端走的那条路。此前冻结评测集",
        "只经 legacy / semantic 两条入口执行，CI 守的是产品已经不用的门。这份报告把同一份",
        "冻结集推过产品入口，并和语义入口并排。",
        "",
        f"- 运行器：`{summary['runner_version']}`",
        f"- 数据集：`{summary['dataset_id']}` v{summary['dataset_version']}",
        f"- 被测入口：`{summary['intent_entrypoint_under_test']}`",
        f"- 工具循环替身版本：`{summary['tool_loop_standin_version']}`；"
        f"语义替身版本：`{summary['semantic_prompt_version']}`（两者共用一个解释器）",
        f"- 门禁：{'PASS' if summary['gate_passed'] else 'FAIL'}",
        "",
        "## D1 工作流用例（产品入口）",
        "",
        f"- 执行：{summary['workflow_cases']['executed']}",
        f"- 通过：{summary['workflow_cases']['passed']}",
        f"- 静默错搜：{len(summary['workflow_cases']['silent_wrong_search_case_ids'])}",
    ]
    for failure in summary["workflow_cases"]["failures"]:
        lines.append(
            f"  - `{failure['case_id']}`（{failure['scenario']}）："
            f"{', '.join(failure['mismatches'])}，观测状态 {failure['observed_state']}"
        )
    lines += [
        "",
        "## D2 意图用例并排对比（语义列为删除前的冻结基线）",
        "",
        "| 指标 | 语义入口（冻结） | 产品入口 | 差值 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for key, row in (summary.get("side_by_side") or {}).items():
        delta = "n/a" if row["delta"] is None else f"{row['delta']:+.4f}"
        note = f" {row['note']}" if row.get("note") else ""
        lines.append(
            f"| `{key}` | {row['semantic']} | {row['agentic']} | {delta} | {row['status']}{note} |"
        )
    lines += ["", "## D2 终态分布", "", "| 状态 | 语义入口 | 产品入口 |", "|---|---:|---:|"]
    semantic_states = summary.get("intent_final_states_semantic") or {}
    agentic_states = summary.get("intent_final_states_agentic") or {}
    for state in sorted(set(semantic_states) | set(agentic_states)):
        lines.append(
            f"| `{state}` | {semantic_states.get(state, 0)} | {agentic_states.get(state, 0)} |"
        )
    lines += ["", "## 限制", ""]
    lines += [f"- {item}" for item in summary["limitations"]]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
