from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_travel_agent.evaluation.regression import (
    BadCaseRecord,
    BaselineMetric,
    RegressionEvaluationError,
    build_bad_case_dataset,
    compare_metric,
    evaluate_regression,
    freeze_regression_baseline,
)

ROOT = Path(__file__).parents[1]
ARTIFACTS = {
    "quality": ROOT
    / "reports/evaluation-runs/phase2-quality-20260802/evaluation-result.json",
    "trajectory": ROOT
    / "reports/evaluation-runs/phase3-trajectory-20260802/trajectory-evaluation-result.json",
    "efficiency": ROOT
    / (
        "reports/evaluation-runs/phase4-tool-efficiency-20260802/"
        "tool-efficiency-evaluation-result.json"
    ),
    "recovery": ROOT
    / (
        "reports/evaluation-runs/phase5-fault-recovery-20260802/"
        "fault-recovery-evaluation-result.json"
    ),
    "performance": ROOT
    / (
        "reports/evaluation-runs/phase6-cost-stability-20260802/analysis/"
        "performance-stability-evaluation-result.json"
    ),
}


def test_bad_case_pipeline_versions_only_reviewed_cases(tmp_path: Path) -> None:
    output = tmp_path / "d6"
    manifest = build_bad_case_dataset(
        phase5_result_path=ARTIFACTS["recovery"],
        fault_cases_path=ROOT / "data/evaluation/fault-eval-v1/cases.jsonl",
        manual_intake_path=ROOT / "evals/intake/manual-bad-cases-v1.jsonl",
        output_directory=output,
    )

    assert manifest.candidate_records == 7
    assert manifest.records == 6
    assert manifest.review_status_counts == {"accepted": 6, "pending": 1}
    accepted = [
        BadCaseRecord.model_validate_json(line)
        for line in (output / "cases.jsonl").read_text().splitlines()
    ]
    assert len(accepted) == 6
    assert all(item.regression_eligible for item in accepted)
    assert "auth-noncanonical" not in " ".join(item.source_case_id for item in accepted)


def test_baseline_keeps_known_failures_and_unknown_cost(tmp_path: Path) -> None:
    target = tmp_path / "baseline.json"
    baseline = freeze_regression_baseline(artifact_paths=ARTIFACTS, output_path=target)

    assert baseline.metrics["recovery.recoverable_fault_success_rate"].value == pytest.approx(
        1 / 3
    )
    assert baseline.metrics["cost.per_successful_task"].value is None
    assert baseline.metrics["cost.per_successful_task"].status == "not_applicable"
    with pytest.raises(RegressionEvaluationError, match="already exists"):
        freeze_regression_baseline(artifact_paths=ARTIFACTS, output_path=target)


def test_initialization_self_check_separates_regression_and_absolute_gates(
    tmp_path: Path,
) -> None:
    baseline_path = tmp_path / "baseline.json"
    freeze_regression_baseline(artifact_paths=ARTIFACTS, output_path=baseline_path)
    summary = evaluate_regression(
        baseline_path=baseline_path,
        current_artifact_paths=ARTIFACTS,
        output_directory=tmp_path / "result",
        comparison_mode="baseline_initialization_self_check",
    )

    assert summary.independent_candidate_run is False
    assert summary.regression_gate_status == "pass"
    assert summary.absolute_gate_status == "fail"
    assert summary.release_gate_status == "fail"
    assert "recovery.recoverable_fault_success_rate" in summary.failed_absolute_metrics
    assert "recovery.partial_result_disclosure_rate" in summary.failed_absolute_metrics
    assert "quality.judge_quality_score" in summary.unevaluated_required_metrics
    assert "stability.real_model_gate" in summary.unevaluated_required_metrics
    assert summary.mutation_detection_rate == 1.0
    assert (tmp_path / "result/evaluation-report.md").is_file()


def test_zero_baseline_regression_is_not_hidden() -> None:
    baseline = BaselineMetric(
        source_stage="trajectory",
        source_metric="tool_hallucination_call_rate",
        status="measured",
        value=0.0,
        unit="rate",
        direction="max",
        absolute_threshold=0.0,
        regression_tolerance=0.0,
        required_for_release=True,
    )
    current = baseline.model_copy(update={"value": 0.01})
    result = compare_metric("trajectory.tool_hallucination_call_rate", baseline, current)
    assert result.absolute_gate == "fail"
    assert result.regression_gate == "fail"
    assert result.relative_regression == float("inf")


def test_unknown_cost_cannot_be_coerced_to_zero() -> None:
    metric = BaselineMetric(
        source_stage="performance",
        source_metric="estimated_cost_per_successful_task",
        status="not_applicable",
        value=None,
        unit="USD",
        direction="max",
        absolute_threshold=None,
        regression_tolerance=0.2,
        required_for_release=False,
    )
    result = compare_metric("cost.per_successful_task", metric, metric)
    assert result.current_value is None
    assert result.regression_gate == "not_evaluated"
    assert "zero substitution" in result.evidence[0]


def test_initialization_self_check_requires_exact_source_hashes(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    freeze_regression_baseline(artifact_paths=ARTIFACTS, output_path=baseline_path)
    changed_quality = tmp_path / "quality.json"
    changed_quality.write_bytes(ARTIFACTS["quality"].read_bytes() + b"\n")
    current = {**ARTIFACTS, "quality": changed_quality}

    with pytest.raises(RegressionEvaluationError, match="differ from the frozen baseline"):
        evaluate_regression(
            baseline_path=baseline_path,
            current_artifact_paths=current,
            output_directory=tmp_path / "result",
            comparison_mode="baseline_initialization_self_check",
        )


def test_candidate_cannot_use_smaller_coverage_with_same_metrics(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    freeze_regression_baseline(artifact_paths=ARTIFACTS, output_path=baseline_path)
    payload = json.loads(ARTIFACTS["quality"].read_text())
    payload["coverage"]["selected_cases"] = 1
    payload["coverage"]["expected_runs"] = 1
    changed_quality = tmp_path / "quality.json"
    changed_quality.write_text(json.dumps(payload))
    current = {**ARTIFACTS, "quality": changed_quality}

    with pytest.raises(RegressionEvaluationError, match="coverage does not match"):
        evaluate_regression(
            baseline_path=baseline_path,
            current_artifact_paths=current,
            output_directory=tmp_path / "result",
            comparison_mode="candidate_regression",
        )


def test_manual_intake_schema_rejects_unreviewed_extra_fields(tmp_path: Path) -> None:
    bad_intake = tmp_path / "bad.jsonl"
    payload = json.loads((ROOT / "evals/intake/manual-bad-cases-v1.jsonl").read_text())
    payload["model_generated_expected_answer"] = "trust me"
    bad_intake.write_text(json.dumps(payload) + "\n")
    with pytest.raises(ValueError):
        build_bad_case_dataset(
            phase5_result_path=ARTIFACTS["recovery"],
            fault_cases_path=ROOT / "data/evaluation/fault-eval-v1/cases.jsonl",
            manual_intake_path=bad_intake,
            output_directory=tmp_path / "d6",
        )
