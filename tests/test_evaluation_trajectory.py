from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_travel_agent.services.evaluation_dataset import (
    WorkflowEvaluationCase,
    load_evaluation_dataset,
)
from corporate_travel_agent.services.evaluation_quality import (
    EvaluationResult,
    WorkflowCaseEvaluation,
)
from corporate_travel_agent.services.evaluation_runner import (
    run_deterministic_workflow_evaluation,
)
from corporate_travel_agent.services.evaluation_trace import EvaluationTrace
from corporate_travel_agent.services.evaluation_trajectory import (
    TrajectoryCaseEvaluation,
    TrajectoryEvaluationError,
    TrajectoryRunSummary,
    evaluate_trajectory_case,
    evaluate_trajectory_run,
    load_tool_registry,
    run_evaluator_mutation_checks,
)

PROJECT_ROOT = Path(__file__).parents[1]
DATASET_ROOT = PROJECT_ROOT / "data" / "evaluation" / "derived-v2"
REGISTRY_PATH = PROJECT_ROOT / "evals" / "contracts" / "tool-registry-v1.json"
DATASET = load_evaluation_dataset(DATASET_ROOT)
REGISTRY = load_tool_registry(REGISTRY_PATH)


def _run_one(
    case_id: str,
    tmp_path: Path,
) -> tuple[WorkflowEvaluationCase, EvaluationTrace, WorkflowCaseEvaluation]:
    source = tmp_path / f"source-{case_id}"
    run_deterministic_workflow_evaluation(
        DATASET_ROOT,
        source,
        case_ids=(case_id,),
    )
    trace = EvaluationTrace.model_validate_json(
        (source / "traces.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    result = WorkflowCaseEvaluation.model_validate_json(
        (source / "case-results.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    case = next(item for item in DATASET.workflow_cases if item.case_id == case_id)
    return case, trace, result


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
def test_trajectory_contract_passes_each_frozen_scenario(
    scenario: str,
    tmp_path: Path,
) -> None:
    case_id = next(case.case_id for case in DATASET.workflow_cases if case.scenario == scenario)
    case, trace, result = _run_one(case_id, tmp_path)

    evaluation = evaluate_trajectory_case(
        case=case,
        trace=trace,
        case_result=result,
        registry=REGISTRY,
    )

    assert evaluation.trajectory_pass is True
    assert all(check.passed for check in evaluation.required_checks)
    assert all(check.passed for check in evaluation.forbidden_checks)
    assert all(check.passed for check in evaluation.order_checks)
    assert all(check.passed for check in evaluation.state_checks)
    assert evaluation.findings == ()


def test_mutation_checks_detect_all_four_hallucination_categories_and_order(
    tmp_path: Path,
) -> None:
    case_id = next(
        case.case_id for case in DATASET.workflow_cases if case.scenario == "COMPLIANT"
    )
    case, trace, result = _run_one(case_id, tmp_path)

    checks = run_evaluator_mutation_checks(
        case=case,
        trace=trace,
        case_result=result,
        registry=REGISTRY,
    )

    assert len(checks) == 5
    assert {item.expected_detection for item in checks} == {
        "tool",
        "parameter_schema",
        "parameter_unsupported",
        "shadow",
        "trajectory_order",
    }
    assert all(item.detected for item in checks)


def test_offline_trajectory_runner_writes_strict_results_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    selected_ids = tuple(
        case.case_id
        for case in DATASET.workflow_cases
        if case.scenario in {"COMPLIANT", "PROVIDER_FAILURE"}
    )[:2]
    source = tmp_path / "source"
    output = tmp_path / "trajectory"
    run_deterministic_workflow_evaluation(
        DATASET_ROOT,
        source,
        case_ids=selected_ids,
    )

    summary = evaluate_trajectory_run(
        dataset_directory=DATASET_ROOT,
        source_run_directory=source,
        output_directory=output,
        tool_registry_path=REGISTRY_PATH,
    )

    assert summary.completed_runs == 2
    assert summary.stage_3_gate_passed is True
    assert summary.full_protocol_gate == "not_evaluated"
    assert all(item.detected for item in summary.mutation_checks)
    case_results = [
        TrajectoryCaseEvaluation.model_validate_json(line)
        for line in (output / "trajectory-case-results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(case_results) == 2
    assert all(item.trajectory_pass for item in case_results)
    formal = EvaluationResult.model_validate_json(
        (output / "trajectory-evaluation-result.json").read_text(encoding="utf-8")
    )
    assert formal.metrics["required_tool_coverage"].value == 1.0
    assert formal.metrics["tool_hallucination_call_rate"].value == 0.0
    assert formal.metrics["parameter_hallucination_call_rate"].value == 0.0
    assert formal.metrics["shadow_hallucination_claim_rate"].value == 0.0
    assert formal.gate_status == "not_evaluated"
    json.loads((output / "trajectory-run-summary.json").read_text(encoding="utf-8"))
    TrajectoryRunSummary.model_validate_json(
        (output / "trajectory-run-summary.json").read_text(encoding="utf-8")
    )

    with pytest.raises(TrajectoryEvaluationError, match="already exists"):
        evaluate_trajectory_run(
            dataset_directory=DATASET_ROOT,
            source_run_directory=source,
            output_directory=output,
            tool_registry_path=REGISTRY_PATH,
        )
