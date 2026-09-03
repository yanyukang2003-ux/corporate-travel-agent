from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_travel_agent.evaluation.dataset import (
    WorkflowEvaluationCase,
    load_evaluation_dataset,
)
from corporate_travel_agent.evaluation.efficiency import (
    EfficiencyEvaluationError,
    EfficiencyRunSummary,
    ToolEfficiencyCaseEvaluation,
    evaluate_tool_efficiency_case,
    evaluate_tool_efficiency_run,
    run_efficiency_mutation_checks,
)
from corporate_travel_agent.evaluation.quality import (
    EvaluationResult,
    WorkflowCaseEvaluation,
)
from corporate_travel_agent.evaluation.runner import (
    run_deterministic_workflow_evaluation,
)
from corporate_travel_agent.evaluation.trace import EvaluationTrace
from corporate_travel_agent.evaluation.trajectory import (
    TrajectoryCaseEvaluation,
    evaluate_trajectory_case,
    evaluate_trajectory_run,
    load_tool_registry,
)

PROJECT_ROOT = Path(__file__).parents[1]
DATASET_ROOT = PROJECT_ROOT / "data" / "evaluation" / "derived-v2"
REGISTRY_PATH = PROJECT_ROOT / "evals" / "contracts" / "tool-registry-v1.json"
DATASET = load_evaluation_dataset(DATASET_ROOT)
REGISTRY = load_tool_registry(REGISTRY_PATH)


def _run_one(
    case_id: str,
    tmp_path: Path,
) -> tuple[
    WorkflowEvaluationCase,
    EvaluationTrace,
    WorkflowCaseEvaluation,
    TrajectoryCaseEvaluation,
]:
    source = tmp_path / f"source-{case_id}"
    run_deterministic_workflow_evaluation(
        DATASET_ROOT,
        source,
        case_ids=(case_id,),
    )
    trace = EvaluationTrace.model_validate_json(
        (source / "traces.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    task_result = WorkflowCaseEvaluation.model_validate_json(
        (source / "case-results.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    case = next(item for item in DATASET.workflow_cases if item.case_id == case_id)
    trajectory_result = evaluate_trajectory_case(
        case=case,
        trace=trace,
        case_result=task_result,
        registry=REGISTRY,
    )
    return case, trace, task_result, trajectory_result


@pytest.mark.parametrize(
    "scenario",
    [
        "COMPLIANT",
        "PREFERENCE_CONFLICT",
        "REQUIRES_APPROVAL",
        "NO_FEASIBLE_OPTION",
        "REVALIDATION_CHANGED",
        "PROVIDER_FAILURE",
    ],
)
def test_minimal_tool_path_is_efficient_for_each_frozen_scenario(
    scenario: str,
    tmp_path: Path,
) -> None:
    case_id = next(case.case_id for case in DATASET.workflow_cases if case.scenario == scenario)
    case, trace, task_result, trajectory_result = _run_one(case_id, tmp_path)

    evaluation = evaluate_tool_efficiency_case(
        case=case,
        trace=trace,
        task_result=task_result,
        trajectory_result=trajectory_result,
    )

    assert evaluation.efficiency_pass is True
    assert evaluation.minimal_path_match is True
    assert evaluation.budget_compliant is True
    assert evaluation.excess_calls == 0
    assert not any(
        call.invalid or call.duplicate or call.redundant or call.no_gain
        for call in evaluation.calls
    )


def test_efficiency_mutations_detect_invalid_duplicate_redundant_and_budget(
    tmp_path: Path,
) -> None:
    case_id = next(
        case.case_id for case in DATASET.workflow_cases if case.scenario == "COMPLIANT"
    )
    case, trace, task_result, _ = _run_one(case_id, tmp_path)

    checks = run_efficiency_mutation_checks(
        case=case,
        trace=trace,
        task_result=task_result,
        registry=REGISTRY,
    )

    assert len(checks) == 6
    assert {item.expected_detection for item in checks} == {
        "invalid_call",
        "duplicate_call",
        "redundant_call",
        "no_gain_call",
        "minimal_path_mismatch",
        "budget_exceeded",
    }
    assert all(item.detected for item in checks)


def test_efficiency_runner_writes_strict_results_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    selected_ids = tuple(
        case.case_id
        for case in DATASET.workflow_cases
        if case.scenario in {"COMPLIANT", "PROVIDER_FAILURE"}
    )[:2]
    source = tmp_path / "source"
    trajectory = tmp_path / "trajectory"
    output = tmp_path / "efficiency"
    run_deterministic_workflow_evaluation(
        DATASET_ROOT,
        source,
        case_ids=selected_ids,
    )
    evaluate_trajectory_run(
        dataset_directory=DATASET_ROOT,
        source_run_directory=source,
        output_directory=trajectory,
        tool_registry_path=REGISTRY_PATH,
    )

    summary = evaluate_tool_efficiency_run(
        dataset_directory=DATASET_ROOT,
        source_run_directory=source,
        trajectory_run_directory=trajectory,
        output_directory=output,
        tool_registry_path=REGISTRY_PATH,
    )

    assert summary.completed_runs == 2
    assert summary.stage_4_gate_passed is True
    assert summary.full_protocol_gate == "not_evaluated"
    assert all(item.detected for item in summary.mutation_checks)
    case_results = [
        ToolEfficiencyCaseEvaluation.model_validate_json(line)
        for line in (output / "tool-efficiency-case-results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(case_results) == 2
    assert all(item.efficiency_pass for item in case_results)
    formal = EvaluationResult.model_validate_json(
        (output / "tool-efficiency-evaluation-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert formal.metrics["invalid_call_rate"].value == 0.0
    assert formal.metrics["duplicate_call_rate"].value == 0.0
    assert formal.metrics["redundant_call_rate"].value == 0.0
    assert formal.metrics["no_gain_call_rate"].value == 0.0
    assert formal.metrics["minimal_path_match_rate"].value == 1.0
    assert formal.metrics["tool_calls_per_passed_task_max"].value == 5.0
    assert formal.gate_status == "not_evaluated"
    json.loads(
        (output / "tool-efficiency-run-summary.json").read_text(encoding="utf-8")
    )
    EfficiencyRunSummary.model_validate_json(
        (output / "tool-efficiency-run-summary.json").read_text(encoding="utf-8")
    )

    with pytest.raises(EfficiencyEvaluationError, match="already exists"):
        evaluate_tool_efficiency_run(
            dataset_directory=DATASET_ROOT,
            source_run_directory=source,
            trajectory_run_directory=trajectory,
            output_directory=output,
            tool_registry_path=REGISTRY_PATH,
        )
