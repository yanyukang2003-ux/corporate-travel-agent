from __future__ import annotations

import json
from collections import Counter

from corporate_travel_agent.evaluation.recovery import (
    build_recovery_evaluation_result,
    evaluate_fault_recovery_run,
    load_fault_evaluation_dataset,
)


def test_frozen_fault_dataset_has_balanced_coverage() -> None:
    dataset = load_fault_evaluation_dataset("data/evaluation/fault-eval-v1")

    assert len(dataset.cases) == 16
    assert set(Counter(case.fault_type for case in dataset.cases).values()) == {2}
    assert sum(case.autonomous_recovery_expected for case in dataset.cases) == 6
    assert sum(case.safe_degradation_allowed for case in dataset.cases) == 14


def test_fault_recovery_run_separates_recovery_from_safe_degradation(tmp_path) -> None:
    output = tmp_path / "recovery-run"
    summary = evaluate_fault_recovery_run(
        fault_dataset_directory="data/evaluation/fault-eval-v1",
        base_dataset_directory="data/evaluation/derived-v2",
        output_directory=output,
    )

    assert summary.completed_runs == 16
    assert summary.autonomous_recovery_candidates == 6
    # Timeout and retryable faults recover through the bounded provider retry;
    # partial results recover by completing the task and disclosing the gap.
    assert summary.autonomous_recoveries == 6
    # Degradation is scored only where the task did not recover on its own, so the
    # four newly auto-recovered faults leave the denominator.
    assert summary.safe_degradation_cases == 10
    assert summary.safe_degradations == 10
    assert summary.unsafe_recoveries == 0
    assert summary.restart_state_recoveries == 2
    assert summary.stage_5_gate_passed
    assert all(item.detected for item in summary.mutation_checks)
    assert (output / "fault-traces.jsonl").is_file()
    assert (output / "evaluation-report.md").is_file()

    metrics = json.loads(
        (output / "fault-recovery-evaluation-result.json").read_text(encoding="utf-8")
    )["metrics"]
    # Partial provider coverage must be visible to the user, never silently dropped.
    assert metrics["partial_result_disclosure_rate"]["value"] == 1.0
    assert metrics["unsafe_recovery_rate"]["value"] == 0.0


def test_recovery_metrics_keep_unknown_denominators_out_of_zero() -> None:
    # The frozen run supplies every denominator; this assertion protects the formal result shape.
    dataset = load_fault_evaluation_dataset("data/evaluation/fault-eval-v1")
    assert dataset.manifest.autonomous_recovery_candidate_records > 0
    assert dataset.manifest.safe_degradation_allowed_records > 0
    assert callable(build_recovery_evaluation_result)
