from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_travel_agent.services.evaluation_observability import (
    MonitoringWindow,
    ObservabilityEvaluationError,
    ObservabilityEvaluationResult,
    ObservationDatasetManifest,
    build_observability_evaluation,
    evaluate_alerts,
)

ROOT = Path(__file__).parents[1]
STAGES = {
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


def _build(tmp_path: Path) -> ObservabilityEvaluationResult:
    return build_observability_evaluation(
        stage_result_paths=STAGES,
        regression_result_path=ROOT
        / "reports/evaluation-runs/phase7-regression-20260802/"
        "regression-evaluation-result.json",
        d6_manifest_path=ROOT / "data/evaluation/bad-case-regression-v1/manifest.json",
        d6_candidates_path=ROOT / "data/evaluation/bad-case-regression-v1/candidates.jsonl",
        baseline_path=ROOT / "evals/baselines/agent-eval-baseline-v1.json",
        price_table_path=ROOT / "evals/pricing/model-prices-unconfigured-v1.json",
        observation_data_directory=tmp_path / "data",
        output_directory=tmp_path / "report",
    )


def test_offline_observation_is_not_misreported_as_production(tmp_path: Path) -> None:
    result = _build(tmp_path)
    manifest = ObservationDatasetManifest.model_validate_json(
        (tmp_path / "data/manifest.json").read_bytes()
    )

    assert result.source_events == 7
    assert result.production_events == 0
    assert result.production_trend_status == "not_evaluated"
    assert manifest.status == "offline_backfill_only"
    assert manifest.production_records == 0
    assert manifest.privacy["raw_user_text_stored"] is False
    assert result.alert_mutation_detection_rate == 1.0


def test_final_summary_keeps_real_api_and_full_score_unavailable(tmp_path: Path) -> None:
    _build(tmp_path)
    payload = json.loads((tmp_path / "report/final-evaluation-summary.json").read_text())

    assert payload["safety_gate"] == "pass"
    assert payload["release_gate"] == "fail"
    assert payload["full_score_status"] == "not_evaluated"
    assert payload["offline_measured_weight"] == 90
    assert payload["offline_earned_points"] == pytest.approx(80.0)
    assert payload["real_api_plan"]["expected_billable_model_calls"] == 72
    assert payload["real_api_plan"]["ready_to_run"] is False
    assert payload["real_api_plan"]["provider_mode"] == "MOCK"


def test_online_alert_rules_trigger_on_thresholds() -> None:
    baseline = MonitoringWindow(
        window_id="baseline",
        production_runs=100,
        task_success_rate=1.0,
        latency_p95_ms=100,
        cost_per_successful_task=1.0,
        safety_events=0,
        failure_signature_counts={},
    )
    windows = tuple(
        MonitoringWindow(
            window_id=f"w{index}",
            production_runs=100,
            task_success_rate=0.94,
            latency_p95_ms=131,
            cost_per_successful_task=1.31,
            safety_events=1 if index == 2 else 0,
            failure_signature_counts={"same": 3},
        )
        for index in (1, 2)
    )
    alerts = evaluate_alerts(
        baseline=baseline, windows=windows, production_coverage=True
    )
    by_type = {item.alert_type: item.status for item in alerts}
    assert by_type["SAFETY_EVENT"] == "triggered"
    assert by_type["TASK_SUCCESS_DROP"] == "triggered"
    assert by_type["LATENCY_REGRESSION"] == "triggered"
    assert by_type["COST_REGRESSION"] == "triggered"
    assert by_type["REPEATED_FAILURE_SIGNATURE"] == "triggered"


def test_unknown_cost_alert_is_not_evaluated() -> None:
    baseline = MonitoringWindow(
        window_id="baseline",
        production_runs=0,
        task_success_rate=1.0,
        latency_p95_ms=100,
        cost_per_successful_task=None,
        safety_events=0,
        failure_signature_counts={},
    )
    window = baseline.model_copy(update={"window_id": "current", "production_runs": 10})
    alerts = evaluate_alerts(
        baseline=baseline,
        windows=(window, window),
        production_coverage=True,
    )
    cost = next(item for item in alerts if item.alert_type == "COST_REGRESSION")
    assert cost.status == "not_evaluated"
    assert cost.actual is None


def test_d6_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    source = ROOT / "data/evaluation/bad-case-regression-v1/candidates.jsonl"
    candidates.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ObservabilityEvaluationError, match="hash does not match"):
        build_observability_evaluation(
            stage_result_paths=STAGES,
            regression_result_path=ROOT
            / "reports/evaluation-runs/phase7-regression-20260802/"
            "regression-evaluation-result.json",
            d6_manifest_path=ROOT / "data/evaluation/bad-case-regression-v1/manifest.json",
            d6_candidates_path=candidates,
            baseline_path=ROOT / "evals/baselines/agent-eval-baseline-v1.json",
            price_table_path=ROOT / "evals/pricing/model-prices-unconfigured-v1.json",
            observation_data_directory=tmp_path / "data",
            output_directory=tmp_path / "report",
        )
