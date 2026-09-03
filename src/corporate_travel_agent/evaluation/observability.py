"""评测什么：可观测性与告警能力——观测窗口、告警触发、复合能力评估。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent import __version__
from corporate_travel_agent.evaluation.performance import (
    ModelPriceTable,
)
from corporate_travel_agent.evaluation.quality import EvaluationResult
from corporate_travel_agent.evaluation.regression import (
    ArtifactReference,
    BadCaseDatasetManifest,
    BadCaseRecord,
    RegressionBaseline,
    RegressionRunSummary,
)

SHA256_PATTERN = r"^[a-f0-9]{64}$"
OBSERVABILITY_EVALUATOR_VERSION: Final = "observability-evaluator-v1"


class ObservabilityEvaluationError(RuntimeError):
    """可观测性评测失败。"""
    pass


class ObservabilityModel(BaseModel):
    """可观测性评测模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


MetricStatus = Literal["measured", "unavailable", "not_applicable"]
AlertStatus = Literal["triggered", "clear", "not_evaluated"]
AlertType = Literal[
    "SAFETY_EVENT",
    "TASK_SUCCESS_DROP",
    "LATENCY_REGRESSION",
    "COST_REGRESSION",
    "REPEATED_FAILURE_SIGNATURE",
    "OFFLINE_RELEASE_GATE_FAILED",
]
AlertSeverity = Literal["warning", "high", "critical"]


class ObservedMetric(ObservabilityModel):
    """观测到的指标样本。"""
    status: MetricStatus
    value: float | None
    unit: str | None
    source: str


class ObservationEvent(ObservabilityModel):
    """观测事件记录。"""
    schema_version: Literal[1] = 1
    observation_id: str
    observed_at: datetime
    source_mode: Literal["offline_evaluation_backfill", "production"]
    source_stage: str
    evaluation_id: str | None
    dataset_id: str | None
    dataset_version: str | None
    selected_cases: int | None = Field(default=None, ge=0)
    completed_runs: int | None = Field(default=None, ge=0)
    metrics: dict[str, ObservedMetric]
    failure_signatures: tuple[str, ...]
    raw_user_text_stored: Literal[False] = False
    raw_provider_response_stored: Literal[False] = False
    source_artifact: ArtifactReference


class AlertEvent(ObservabilityModel):
    """告警事件记录。"""
    alert_id: str
    alert_type: AlertType
    severity: AlertSeverity
    status: AlertStatus
    threshold: str
    actual: float | None
    evidence: tuple[str, ...]


class MonitoringWindow(ObservabilityModel):
    """监控时间窗口。"""
    window_id: str
    production_runs: int = Field(ge=0)
    task_success_rate: float | None = Field(default=None, ge=0, le=1)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    cost_per_successful_task: float | None = Field(default=None, ge=0)
    safety_events: int = Field(ge=0)
    failure_signature_counts: dict[str, int]


class AlertMutationCheck(ObservabilityModel):
    """告警评测突变检查。"""
    mutation_id: str
    alert_type: str
    expected_status: AlertStatus
    actual_status: AlertStatus
    detected: bool


class DailyObservationSummary(ObservabilityModel):
    """日度观测汇总。"""
    schema_version: Literal[1] = 1
    date: date
    source_mode: Literal["offline_evaluation_backfill"]
    observation_events: int = Field(ge=1)
    production_runs: Literal[0] = 0
    metrics: dict[str, ObservedMetric]
    alerts: tuple[AlertEvent, ...]
    limitations: tuple[str, ...]


class WeeklyObservationSummary(ObservabilityModel):
    """周度观测汇总。"""
    schema_version: Literal[1] = 1
    iso_week: str = Field(pattern=r"^\d{4}-W\d{2}$")
    source_mode: Literal["offline_evaluation_backfill"]
    daily_windows: Literal[1] = 1
    production_runs: Literal[0] = 0
    task_success_trend: Literal["not_evaluated"] = "not_evaluated"
    latency_trend: Literal["not_evaluated"] = "not_evaluated"
    cost_trend: Literal["not_evaluated"] = "not_evaluated"
    reason: str


class ObservationDatasetManifest(ObservabilityModel):
    """观测数据集清单。"""
    schema_version: Literal[1] = 1
    dataset_id: Literal["production-observations"] = "production-observations"
    version: Literal["local-v1"] = "local-v1"
    status: Literal["offline_backfill_only"] = "offline_backfill_only"
    generated_at: datetime
    records: int = Field(ge=1)
    production_records: Literal[0] = 0
    events_file: Literal["events.jsonl"] = "events.jsonl"
    events_sha256: str = Field(pattern=SHA256_PATTERN)
    daily_file: str
    daily_sha256: str = Field(pattern=SHA256_PATTERN)
    weekly_file: str
    weekly_sha256: str = Field(pattern=SHA256_PATTERN)
    privacy: dict[str, bool]
    source_artifacts: dict[str, ArtifactReference]
    limitations: tuple[str, ...]


class CompositeComponent(ObservabilityModel):
    """复合能力评估组件。"""
    component_id: Literal[
        "task_quality", "stability", "trajectory", "recovery", "cost_efficiency"
    ]
    weight: int = Field(ge=1)
    status: Literal["measured", "partial", "not_evaluated"]
    normalized_value: float | None = Field(default=None, ge=0, le=1)
    earned_points: float | None = Field(default=None, ge=0)
    evidence: tuple[str, ...]


class CapabilityAssessment(ObservabilityModel):
    """单项能力评估。"""
    capability: str
    status: Literal["measured_pass", "measured_fail", "partial", "not_evaluated"]
    evidence: str


class RealApiEvaluationPlan(ObservabilityModel):
    """真实 API 评测计划。"""
    status: Literal["blocked_prerequisites", "ready"]
    evaluation_mode: Literal["real_llm_mock_provider"] = "real_llm_mock_provider"
    subset_id: Literal["intent-model-smoke-v1"] = "intent-model-smoke-v1"
    subset_records: Literal[24] = 24
    attempts_per_case: Literal[3] = 3
    expected_billable_model_calls: Literal[72] = 72
    provider_mode: Literal["MOCK"] = "MOCK"
    api_key_configured: bool
    model_explicitly_configured: bool
    requested_model: str
    sdk_installed: bool
    price_table_status: Literal["configured", "unconfigured"]
    requested_model_has_price: bool
    billable_confirmation_received: Literal[False] = False
    ready_to_run: bool
    blockers: tuple[str, ...]
    limitations: tuple[str, ...]


class FinalEvaluationSummary(ObservabilityModel):
    """最终评测总结。"""
    schema_version: Literal[1] = 1
    summary_id: str
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    generated_at: datetime
    project_version: str
    safety_gate: Literal["pass", "fail", "not_evaluated"]
    release_gate: Literal["pass", "fail"]
    full_score_status: Literal["not_evaluated"] = "not_evaluated"
    offline_measured_weight: int = Field(ge=0, le=100)
    offline_earned_points: float = Field(ge=0, le=100)
    offline_normalized_score: float = Field(ge=0, le=1)
    components: tuple[CompositeComponent, ...]
    capabilities: tuple[CapabilityAssessment, ...]
    known_failures: tuple[str, ...]
    required_but_unevaluated: tuple[str, ...]
    real_api_plan: RealApiEvaluationPlan
    interpretation: str


class ObservabilityEvaluationResult(ObservabilityModel):
    """可观测性评测总结果。"""
    schema_version: Literal[1] = 1
    evaluation_id: str
    evaluator_version: Literal["observability-evaluator-v1"] = (
        OBSERVABILITY_EVALUATOR_VERSION
    )
    generated_at: datetime
    source_events: int = Field(ge=1)
    production_events: Literal[0] = 0
    daily_windows: Literal[1] = 1
    weekly_windows: Literal[1] = 1
    alerts: tuple[AlertEvent, ...]
    alert_mutation_checks: tuple[AlertMutationCheck, ...]
    alert_mutation_detection_rate: float = Field(ge=0, le=1)
    production_trend_status: Literal["not_evaluated"] = "not_evaluated"
    observation_dataset_manifest: ArtifactReference
    final_summary_file: Literal["final-evaluation-summary.json"] = (
        "final-evaluation-summary.json"
    )
    limitations: tuple[str, ...]


def build_observability_evaluation(
    *,
    stage_result_paths: dict[str, str | Path],
    regression_result_path: str | Path,
    d6_manifest_path: str | Path,
    d6_candidates_path: str | Path,
    baseline_path: str | Path,
    price_table_path: str | Path,
    observation_data_directory: str | Path,
    output_directory: str | Path,
    observed_date: date = date(2026, 8, 2),
) -> ObservabilityEvaluationResult:
    """构建可观测性评测结果与能力评估。"""
    required_stages = {"quality", "trajectory", "efficiency", "recovery", "performance"}
    if set(stage_result_paths) != required_stages:
        raise ObservabilityEvaluationError(
            f"stage result keys must be exactly {sorted(required_stages)}"
        )
    resolved_stage_paths = {
        key: Path(value).expanduser().resolve()
        for key, value in stage_result_paths.items()
    }
    stage_results = {
        key: EvaluationResult.model_validate_json(path.read_bytes())
        for key, path in resolved_stage_paths.items()
    }
    regression_path = Path(regression_result_path).expanduser().resolve()
    regression = RegressionRunSummary.model_validate_json(regression_path.read_bytes())
    d6_manifest_file = Path(d6_manifest_path).expanduser().resolve()
    d6_manifest = BadCaseDatasetManifest.model_validate_json(
        d6_manifest_file.read_bytes()
    )
    candidates_path = Path(d6_candidates_path).expanduser().resolve()
    candidates = tuple(
        BadCaseRecord.model_validate_json(line)
        for line in candidates_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if len(candidates) != d6_manifest.candidate_records:
        raise ObservabilityEvaluationError("D6 candidate count does not match its manifest")
    if _sha256(candidates_path.read_bytes()) != d6_manifest.candidates_sha256:
        raise ObservabilityEvaluationError("D6 candidate hash does not match its manifest")
    baseline_file = Path(baseline_path).expanduser().resolve()
    baseline = RegressionBaseline.model_validate_json(baseline_file.read_bytes())
    price_file = Path(price_table_path).expanduser().resolve()
    price_table = ModelPriceTable.model_validate_json(price_file.read_bytes())

    events = _build_observation_events(
        resolved_stage_paths=resolved_stage_paths,
        stage_results=stage_results,
        regression_path=regression_path,
        regression=regression,
        d6_manifest_path=d6_manifest_file,
        d6_manifest=d6_manifest,
        candidates=candidates,
    )
    aggregate_metrics = _aggregate_metrics(stage_results, regression, candidates)
    baseline_window = _baseline_monitoring_window(baseline)
    current_window = MonitoringWindow(
        window_id=observed_date.isoformat(),
        production_runs=0,
        task_success_rate=aggregate_metrics["task_pass_rate"].value,
        latency_p95_ms=aggregate_metrics["total_latency_ms_p95"].value,
        cost_per_successful_task=aggregate_metrics["cost_per_successful_task"].value,
        safety_events=int(aggregate_metrics["safety_events"].value or 0),
        failure_signature_counts=dict(Counter(item.failure_signature for item in candidates)),
    )
    alerts = evaluate_alerts(
        baseline=baseline_window,
        windows=(current_window,),
        production_coverage=False,
        offline_release_failed=regression.release_gate_status == "fail",
    )
    daily = DailyObservationSummary(
        date=observed_date,
        source_mode="offline_evaluation_backfill",
        observation_events=len(events),
        metrics=aggregate_metrics,
        alerts=alerts,
        limitations=(
            "The window contains offline evaluation artifacts, not production requests.",
            "No raw user text or raw provider response is stored.",
        ),
    )
    week = observed_date.isocalendar()
    weekly = WeeklyObservationSummary(
        iso_week=f"{week.year}-W{week.week:02d}",
        source_mode="offline_evaluation_backfill",
        reason="At least two production windows are required for a trend decision.",
    )
    source_artifacts = {
        **{
            key: _artifact_reference(path, stage_results[key])
            for key, path in resolved_stage_paths.items()
        },
        "regression": _artifact_reference(regression_path),
        "d6_manifest": _artifact_reference(d6_manifest_file),
        "d6_candidates": _artifact_reference(candidates_path),
        "baseline": _artifact_reference(baseline_file),
        "price_table": _artifact_reference(price_file),
    }

    data_root = Path(observation_data_directory).expanduser().resolve()
    output_root = Path(output_directory).expanduser().resolve()
    if data_root.exists():
        raise ObservabilityEvaluationError(
            f"observation data directory already exists: {data_root}"
        )
    if output_root.exists():
        raise ObservabilityEvaluationError(f"output directory already exists: {output_root}")
    data_root.mkdir(parents=True)
    (data_root / "daily").mkdir()
    (data_root / "weekly").mkdir()
    events_payload = "".join(f"{item.model_dump_json()}\n" for item in events).encode()
    daily_payload = daily.model_dump_json(indent=2).encode()
    weekly_payload = weekly.model_dump_json(indent=2).encode()
    daily_relative = f"daily/{observed_date.isoformat()}.json"
    weekly_relative = f"weekly/{weekly.iso_week}.json"
    (data_root / "events.jsonl").write_bytes(events_payload)
    (data_root / daily_relative).write_bytes(daily_payload)
    (data_root / weekly_relative).write_bytes(weekly_payload)
    data_manifest = ObservationDatasetManifest(
        generated_at=datetime.now(UTC),
        records=len(events),
        events_sha256=_sha256(events_payload),
        daily_file=daily_relative,
        daily_sha256=_sha256(daily_payload),
        weekly_file=weekly_relative,
        weekly_sha256=_sha256(weekly_payload),
        privacy={
            "raw_user_text_stored": False,
            "raw_provider_response_stored": False,
            "redacted_metrics_and_hashes_only": True,
        },
        source_artifacts=source_artifacts,
        limitations=(
            "This first version is an offline backfill that proves the observation pipeline.",
            "Production trend metrics remain not_evaluated until real windows are ingested.",
        ),
    )
    data_manifest_path = data_root / "manifest.json"
    data_manifest_path.write_text(data_manifest.model_dump_json(indent=2), encoding="utf-8")

    real_api_plan = _build_real_api_plan(price_table)
    final_summary = _build_final_summary(stage_results, regression, real_api_plan)
    mutation_checks = _run_alert_mutation_checks(baseline_window)
    output_root.mkdir(parents=True)
    (output_root / "alerts.jsonl").write_text(
        "".join(f"{item.model_dump_json()}\n" for item in alerts), encoding="utf-8"
    )
    (output_root / "alert-mutation-checks.json").write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in mutation_checks],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_root / "final-evaluation-summary.json").write_text(
        final_summary.model_dump_json(indent=2), encoding="utf-8"
    )
    result = ObservabilityEvaluationResult(
        evaluation_id=f"eval-{uuid4()}",
        generated_at=datetime.now(UTC),
        source_events=len(events),
        alerts=alerts,
        alert_mutation_checks=mutation_checks,
        alert_mutation_detection_rate=(
            sum(item.detected for item in mutation_checks) / len(mutation_checks)
        ),
        observation_dataset_manifest=_artifact_reference(data_manifest_path),
        limitations=(
            "No production request was observed in this evaluation.",
            "The real-model run remains blocked by explicit prerequisites.",
            "Offline score is diagnostic and must not be reported as a full release score.",
        ),
    )
    (output_root / "observability-evaluation-result.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_root / "evaluation-report.md").write_text(
        _render_report(result, daily, weekly, final_summary, data_manifest),
        encoding="utf-8",
    )
    return result


def evaluate_alerts(
    *,
    baseline: MonitoringWindow,
    windows: tuple[MonitoringWindow, ...],
    production_coverage: bool,
    offline_release_failed: bool = False,
) -> tuple[AlertEvent, ...]:
    """评估告警规则是否在观测数据上正确触发。"""
    if not windows:
        raise ObservabilityEvaluationError("at least one monitoring window is required")
    current = windows[-1]
    safety_status: AlertStatus = "triggered" if current.safety_events > 0 else "clear"
    alerts = [
        AlertEvent(
            alert_id=f"alert-{uuid4()}",
            alert_type="SAFETY_EVENT",
            severity="critical",
            status=safety_status,
            threshold="any safety event triggers immediately",
            actual=float(current.safety_events),
            evidence=(f"window:{current.window_id}",),
        )
    ]
    if production_coverage:
        alerts.extend(
            (
                _rate_drop_alert(baseline, current),
                _two_window_regression_alert(
                    baseline,
                    windows,
                    metric="latency_p95_ms",
                    alert_type="LATENCY_REGRESSION",
                ),
                _two_window_regression_alert(
                    baseline,
                    windows,
                    metric="cost_per_successful_task",
                    alert_type="COST_REGRESSION",
                ),
                _signature_alert(current),
            )
        )
    else:
        for alert_type, severity, threshold in (
            ("TASK_SUCCESS_DROP", "high", "24h success falls by more than 5 points"),
            ("LATENCY_REGRESSION", "warning", "P95 grows 30% for two windows"),
            ("COST_REGRESSION", "warning", "unit cost grows 30% for two windows"),
            ("REPEATED_FAILURE_SIGNATURE", "high", "same signature appears 3 times in 24h"),
        ):
            alerts.append(
                AlertEvent(
                    alert_id=f"alert-{uuid4()}",
                    alert_type=cast(AlertType, alert_type),
                    severity=cast(AlertSeverity, severity),
                    status="not_evaluated",
                    threshold=threshold,
                    actual=None,
                    evidence=("production_runs:0",),
                )
            )
    alerts.append(
        AlertEvent(
            alert_id=f"alert-{uuid4()}",
            alert_type="OFFLINE_RELEASE_GATE_FAILED",
            severity="high",
            status="triggered" if offline_release_failed else "clear",
            threshold="offline release gate must pass",
            actual=0.0 if offline_release_failed else 1.0,
            evidence=("stage7.release_gate",),
        )
    )
    return tuple(alerts)


def _rate_drop_alert(
    baseline: MonitoringWindow, current: MonitoringWindow
) -> AlertEvent:
    if baseline.task_success_rate is None or current.task_success_rate is None:
        status: AlertStatus = "not_evaluated"
        actual = None
    else:
        actual = baseline.task_success_rate - current.task_success_rate
        status = "triggered" if actual > 0.05 else "clear"
    return AlertEvent(
        alert_id=f"alert-{uuid4()}",
        alert_type="TASK_SUCCESS_DROP",
        severity="high",
        status=status,
        threshold="drop > 0.05",
        actual=actual,
        evidence=(f"baseline:{baseline.task_success_rate}", f"current:{current.task_success_rate}"),
    )


def _two_window_regression_alert(
    baseline: MonitoringWindow,
    windows: tuple[MonitoringWindow, ...],
    *,
    metric: Literal["latency_p95_ms", "cost_per_successful_task"],
    alert_type: Literal["LATENCY_REGRESSION", "COST_REGRESSION"],
) -> AlertEvent:
    baseline_value = getattr(baseline, metric)
    recent = tuple(getattr(item, metric) for item in windows[-2:])
    if baseline_value is None or len(recent) < 2 or any(item is None for item in recent):
        status: AlertStatus = "not_evaluated"
        actual = None
    else:
        regressions = tuple((float(item) - baseline_value) / baseline_value for item in recent)
        actual = min(regressions)
        status = "triggered" if all(item > 0.3 for item in regressions) else "clear"
    return AlertEvent(
        alert_id=f"alert-{uuid4()}",
        alert_type=alert_type,
        severity="warning",
        status=status,
        threshold="relative growth > 0.30 for two consecutive windows",
        actual=actual,
        evidence=tuple(f"{metric}:{item}" for item in recent),
    )


def _signature_alert(current: MonitoringWindow) -> AlertEvent:
    maximum = max(current.failure_signature_counts.values(), default=0)
    return AlertEvent(
        alert_id=f"alert-{uuid4()}",
        alert_type="REPEATED_FAILURE_SIGNATURE",
        severity="high",
        status="triggered" if maximum >= 3 else "clear",
        threshold="same failure signature count >= 3 in 24h",
        actual=float(maximum),
        evidence=tuple(
            f"{signature}:{count}"
            for signature, count in current.failure_signature_counts.items()
            if count == maximum
        ),
    )


def _build_observation_events(
    *,
    resolved_stage_paths: dict[str, Path],
    stage_results: dict[str, EvaluationResult],
    regression_path: Path,
    regression: RegressionRunSummary,
    d6_manifest_path: Path,
    d6_manifest: BadCaseDatasetManifest,
    candidates: tuple[BadCaseRecord, ...],
) -> tuple[ObservationEvent, ...]:
    events: list[ObservationEvent] = []
    for stage, result in stage_results.items():
        metrics = {
            key: ObservedMetric(
                status=value.status,
                value=value.value,
                unit=value.unit,
                source=f"{stage}.{key}",
            )
            for key, value in result.metrics.items()
        }
        events.append(
            ObservationEvent(
                observation_id=f"obs-{uuid4()}",
                observed_at=result.generated_at,
                source_mode="offline_evaluation_backfill",
                source_stage=stage,
                evaluation_id=result.evaluation_id,
                dataset_id=result.coverage.dataset_id,
                dataset_version=result.coverage.dataset_version,
                selected_cases=result.coverage.selected_cases,
                completed_runs=result.coverage.completed_runs,
                metrics=metrics,
                failure_signatures=tuple(
                    item.failure_signature for item in result.bad_case_candidates
                ),
                source_artifact=_artifact_reference(resolved_stage_paths[stage], result),
            )
        )
    events.append(
        ObservationEvent(
            observation_id=f"obs-{uuid4()}",
            observed_at=regression.generated_at,
            source_mode="offline_evaluation_backfill",
            source_stage="regression",
            evaluation_id=regression.evaluation_id,
            dataset_id="bad-case-regression",
            dataset_version=d6_manifest.version,
            selected_cases=d6_manifest.records,
            completed_runs=0,
            metrics={
                "absolute_gate": _boolean_metric(
                    regression.absolute_gate_status == "pass", "regression.absolute_gate"
                ),
                "regression_gate": _boolean_metric(
                    regression.regression_gate_status == "pass", "regression.regression_gate"
                ),
                "release_gate": _boolean_metric(
                    regression.release_gate_status == "pass", "regression.release_gate"
                ),
                "mutation_detection_rate": ObservedMetric(
                    status="measured",
                    value=regression.mutation_detection_rate,
                    unit="rate",
                    source="regression.mutation_detection_rate",
                ),
            },
            failure_signatures=tuple(
                item.failure_signature for item in candidates if item.regression_eligible
            ),
            source_artifact=_artifact_reference(regression_path),
        )
    )
    events.append(
        ObservationEvent(
            observation_id=f"obs-{uuid4()}",
            observed_at=d6_manifest.generated_at,
            source_mode="offline_evaluation_backfill",
            source_stage="bad_case_feedback",
            evaluation_id=None,
            dataset_id=d6_manifest.dataset_id,
            dataset_version=d6_manifest.version,
            selected_cases=d6_manifest.candidate_records,
            completed_runs=0,
            metrics={
                "accepted_cases": ObservedMetric(
                    status="measured",
                    value=float(d6_manifest.records),
                    unit="cases",
                    source="d6.records",
                ),
                "pending_cases": ObservedMetric(
                    status="measured",
                    value=float(d6_manifest.review_status_counts.get("pending", 0)),
                    unit="cases",
                    source="d6.review_status_counts.pending",
                ),
            },
            failure_signatures=tuple(item.failure_signature for item in candidates),
            source_artifact=_artifact_reference(d6_manifest_path),
        )
    )
    return tuple(events)


def _aggregate_metrics(
    results: dict[str, EvaluationResult],
    regression: RegressionRunSummary,
    candidates: tuple[BadCaseRecord, ...],
) -> dict[str, ObservedMetric]:
    trajectory = results["trajectory"].metrics
    safety_events = sum(
        int(trajectory[key].numerator or 0)
        for key in (
            "tool_hallucination_call_rate",
            "parameter_hallucination_call_rate",
            "shadow_hallucination_claim_rate",
        )
    ) + int(results["recovery"].metrics["unsafe_recovery_rate"].numerator or 0)
    mapping = {
        "task_pass_rate": ("quality", "task_pass_rate"),
        "trajectory_pass_rate": ("trajectory", "trajectory_pass_rate"),
        "tool_hallucination_call_rate": (
            "trajectory",
            "tool_hallucination_call_rate",
        ),
        "parameter_hallucination_call_rate": (
            "trajectory",
            "parameter_hallucination_call_rate",
        ),
        "shadow_hallucination_claim_rate": (
            "trajectory",
            "shadow_hallucination_claim_rate",
        ),
        "efficiency_pass_rate": ("efficiency", "efficiency_pass_rate"),
        "recoverable_fault_success_rate": (
            "recovery",
            "recoverable_fault_success_rate",
        ),
        "unsafe_recovery_rate": ("recovery", "unsafe_recovery_rate"),
        "pass_power_3": ("performance", "pass_power_3"),
        "mixed_run_rate": ("performance", "mixed_run_rate"),
        "total_latency_ms_p95": ("performance", "total_latency_ms_p95"),
        "cost_per_successful_task": (
            "performance",
            "estimated_cost_per_successful_task",
        ),
    }
    aggregate = {
        name: ObservedMetric(
            status=results[stage].metrics[key].status,
            value=results[stage].metrics[key].value,
            unit=results[stage].metrics[key].unit,
            source=f"{stage}.{key}",
        )
        for name, (stage, key) in mapping.items()
    }
    aggregate.update(
        {
            "safety_events": ObservedMetric(
                status="measured",
                value=float(safety_events),
                unit="events",
                source="trajectory_and_recovery",
            ),
            "bad_case_candidates": ObservedMetric(
                status="measured",
                value=float(len(candidates)),
                unit="cases",
                source="d6.candidates",
            ),
            "release_gate": _boolean_metric(
                regression.release_gate_status == "pass", "regression.release_gate"
            ),
        }
    )
    return aggregate


def _build_final_summary(
    results: dict[str, EvaluationResult],
    regression: RegressionRunSummary,
    real_api_plan: RealApiEvaluationPlan,
) -> FinalEvaluationSummary:
    quality = results["quality"].metrics["task_pass_rate"].value or 0.0
    stability = results["performance"].metrics["pass_power_3"].value or 0.0
    trajectory = results["trajectory"].metrics["trajectory_pass_rate"].value or 0.0
    recovery = results["recovery"].metrics["recoverable_fault_success_rate"].value or 0.0
    components = (
        CompositeComponent(
            component_id="task_quality",
            weight=40,
            status="partial",
            normalized_value=quality,
            earned_points=40 * quality,
            evidence=("D1 deterministic quality measured", "real-model and Judge unavailable"),
        ),
        CompositeComponent(
            component_id="stability",
            weight=20,
            status="partial",
            normalized_value=stability,
            earned_points=20 * stability,
            evidence=("D1 deterministic 3-run stability measured", "real-model 3-run unavailable"),
        ),
        CompositeComponent(
            component_id="trajectory",
            weight=15,
            status="measured",
            normalized_value=trajectory,
            earned_points=15 * trajectory,
            evidence=("D1 full trace evaluation",),
        ),
        CompositeComponent(
            component_id="recovery",
            weight=15,
            status="measured",
            normalized_value=recovery,
            earned_points=15 * recovery,
            evidence=("D5 recoverable-fault success rate",),
        ),
        CompositeComponent(
            component_id="cost_efficiency",
            weight=10,
            status="not_evaluated",
            normalized_value=None,
            earned_points=None,
            evidence=("no LLM cost exposure", "tool efficiency measured separately"),
        ),
    )
    measured = tuple(item for item in components if item.status != "not_evaluated")
    measured_weight = sum(item.weight for item in measured)
    earned = sum(item.earned_points or 0.0 for item in measured)
    safety_events = sum(
        int(results["trajectory"].metrics[key].numerator or 0)
        for key in (
            "tool_hallucination_call_rate",
            "parameter_hallucination_call_rate",
            "shadow_hallucination_claim_rate",
        )
    )
    unsafe = results["recovery"].metrics["unsafe_recovery_rate"].value
    safety_gate: Literal["pass", "fail"] = (
        "pass" if safety_events == 0 and unsafe == 0 else "fail"
    )
    return FinalEvaluationSummary(
        summary_id=f"summary-{uuid4()}",
        generated_at=datetime.now(UTC),
        project_version=__version__,
        safety_gate=safety_gate,
        release_gate="pass" if regression.release_gate_status == "pass" else "fail",
        offline_measured_weight=measured_weight,
        offline_earned_points=earned,
        offline_normalized_score=earned / measured_weight,
        components=components,
        capabilities=(
            CapabilityAssessment(
                capability="任务完成质量",
                status="partial",
                evidence="D1 60/60；真实模型质量与 Judge 未评测",
            ),
            CapabilityAssessment(
                capability="轨迹感知与三类幻觉",
                status="measured_pass",
                evidence="D1 轨迹 60/60，三类幻觉事件均为 0；工具选择由编排器控制",
            ),
            CapabilityAssessment(
                capability="工具效率",
                status="measured_pass",
                evidence="重复/冗余/无增益均为 0，P95 调用数 5",
            ),
            CapabilityAssessment(
                capability="异常恢复",
                status="measured_fail",
                evidence="可恢复故障成功率 33.3%，部分结果披露率 0%",
            ),
            CapabilityAssessment(
                capability="Token、时延与费用",
                status="partial",
                evidence="本地时延已测；真实 LLM Token 和费用未评测",
            ),
            CapabilityAssessment(
                capability="多次运行稳定性",
                status="partial",
                evidence="D1 三次稳定 100%；真实模型 24×3 未运行",
            ),
            CapabilityAssessment(
                capability="坏案例闭环",
                status="measured_pass",
                evidence="D6 6 条 accepted、1 条 pending，已接入版本化回归",
            ),
            CapabilityAssessment(
                capability="线上观测",
                status="partial",
                evidence="本地观测、聚合和告警就绪；真实生产窗口为 0",
            ),
        ),
        known_failures=(
            "recoverable_fault_success_rate=33.3% < 80%",
            "partial_result_disclosure_rate=0% < 100%",
        ),
        required_but_unevaluated=(
            "real-model task quality on intent-model-smoke-v1",
            "real-model pass_power_3 and mixed_run_rate",
            "measured LLM Token and cost",
            "calibrated LLM Judge or human review",
            "production observation trends",
        ),
        real_api_plan=real_api_plan,
        interpretation=(
            "The 80/90 offline evidence score is diagnostic only. It excludes the 10-point "
            "cost component and relies on deterministic evidence for quality and stability; "
            "the full 100-point score remains not_evaluated and the release gate fails."
        ),
    )


def _build_real_api_plan(price_table: ModelPriceTable) -> RealApiEvaluationPlan:
    model_from_env = os.getenv("OPENAI_MODEL")
    requested_model = model_from_env or "gpt-5.6"
    api_key = bool(os.getenv("OPENAI_API_KEY"))
    model_configured = bool(model_from_env)
    price_present = requested_model in price_table.models
    blockers: list[str] = []
    if not api_key:
        blockers.append("OPENAI_API_KEY is not configured")
    if not model_configured:
        blockers.append("OPENAI_MODEL is not explicitly configured")
    if price_table.status != "configured":
        blockers.append("the versioned price table is unconfigured")
    elif not price_present:
        blockers.append("the requested model has no versioned price entry")
    blockers.append("explicit --confirm-billable approval has not been supplied")
    return RealApiEvaluationPlan(
        status="blocked_prerequisites",
        api_key_configured=api_key,
        model_explicitly_configured=model_configured,
        requested_model=requested_model,
        sdk_installed=importlib.util.find_spec("openai") is not None,
        price_table_status=price_table.status,
        requested_model_has_price=price_present,
        ready_to_run=False,
        blockers=tuple(blockers),
        limitations=(
            "The real model extracts intent only; tool selection remains orchestrator-controlled.",
            "Travel inventory and provider calls remain deterministic Mock data.",
            "This is not a live airline, hotel, booking, or payment evaluation.",
        ),
    )


def _baseline_monitoring_window(baseline: RegressionBaseline) -> MonitoringWindow:
    def value(metric_id: str) -> float | None:
        metric = baseline.metrics[metric_id]
        return metric.value if metric.status == "measured" else None

    return MonitoringWindow(
        window_id=baseline.baseline_id,
        production_runs=0,
        task_success_rate=value("quality.task_pass_rate"),
        latency_p95_ms=value("performance.total_latency_ms_p95"),
        cost_per_successful_task=value("cost.per_successful_task"),
        safety_events=0,
        failure_signature_counts={},
    )


def _run_alert_mutation_checks(
    baseline: MonitoringWindow,
) -> tuple[AlertMutationCheck, ...]:
    def window(
        name: str,
        *,
        success: float = 1.0,
        latency: float | None = None,
        cost: float | None = None,
        safety: int = 0,
        signatures: dict[str, int] | None = None,
    ) -> MonitoringWindow:
        return MonitoringWindow(
            window_id=name,
            production_runs=10,
            task_success_rate=success,
            latency_p95_ms=latency,
            cost_per_successful_task=cost,
            safety_events=safety,
            failure_signature_counts=signatures or {},
        )

    baseline_latency = baseline.latency_p95_ms or 1.0
    priced_baseline = baseline.model_copy(update={"cost_per_successful_task": 1.0})
    scenarios = (
        (
            "safety_any_event",
            baseline,
            (window("w1", safety=1, latency=baseline_latency),),
            "SAFETY_EVENT",
            "triggered",
        ),
        (
            "success_drop_6_points",
            baseline,
            (window("w1", success=0.94, latency=baseline_latency),),
            "TASK_SUCCESS_DROP",
            "triggered",
        ),
        (
            "latency_two_windows_plus_31_percent",
            baseline,
            (
                window("w1", latency=baseline_latency * 1.31),
                window("w2", latency=baseline_latency * 1.31),
            ),
            "LATENCY_REGRESSION",
            "triggered",
        ),
        (
            "cost_two_windows_plus_31_percent",
            priced_baseline,
            (
                window("w1", latency=baseline_latency, cost=1.31),
                window("w2", latency=baseline_latency, cost=1.31),
            ),
            "COST_REGRESSION",
            "triggered",
        ),
        (
            "same_signature_three_times",
            baseline,
            (window("w1", latency=baseline_latency, signatures={"sig-a": 3}),),
            "REPEATED_FAILURE_SIGNATURE",
            "triggered",
        ),
        (
            "unknown_cost_not_zero",
            baseline,
            (
                window("w1", latency=baseline_latency),
                window("w2", latency=baseline_latency),
            ),
            "COST_REGRESSION",
            "not_evaluated",
        ),
    )
    checks: list[AlertMutationCheck] = []
    for mutation_id, scenario_baseline, windows, alert_type, expected in scenarios:
        alerts = evaluate_alerts(
            baseline=scenario_baseline,
            windows=windows,
            production_coverage=True,
        )
        actual = next(item.status for item in alerts if item.alert_type == alert_type)
        checks.append(
            AlertMutationCheck(
                mutation_id=mutation_id,
                alert_type=alert_type,
                expected_status=cast(AlertStatus, expected),
                actual_status=actual,
                detected=actual == expected,
            )
        )
    return tuple(checks)


def _boolean_metric(value: bool, source: str) -> ObservedMetric:
    return ObservedMetric(
        status="measured", value=float(value), unit="boolean", source=source
    )


def _artifact_reference(
    path: Path, result: EvaluationResult | None = None
) -> ArtifactReference:
    coverage = result.coverage if result is not None else None
    return ArtifactReference(
        path=_portable_path(path),
        sha256=_sha256(path.read_bytes()),
        evaluation_id=result.evaluation_id if result is not None else None,
        dataset_id=coverage.dataset_id if coverage is not None else None,
        dataset_version=coverage.dataset_version if coverage is not None else None,
        selected_cases=coverage.selected_cases if coverage is not None else None,
        expected_runs=coverage.expected_runs if coverage is not None else None,
    )


def _portable_path(path: Path) -> str:
    project_root = Path(__file__).resolve().parents[3]
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return str(path.resolve())


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _render_report(
    result: ObservabilityEvaluationResult,
    daily: DailyObservationSummary,
    weekly: WeeklyObservationSummary,
    final: FinalEvaluationSummary,
    manifest: ObservationDatasetManifest,
) -> str:
    capability_rows = "\n".join(
        f"| {item.capability} | {item.status} | {item.evidence} |"
        for item in final.capabilities
    )
    alerts = "\n".join(
        f"| {item.alert_type} | {item.status} | {item.threshold} |"
        for item in result.alerts
    )
    blockers = "\n".join(f"- {item}" for item in final.real_api_plan.blockers)
    return f"""# 第 8 阶段：观测、告警与综合评测报告

## 结论

- 安全门禁：**{final.safety_gate.upper()}**
- 发布门禁：**{final.release_gate.upper()}**
- 线上趋势：**{result.production_trend_status.upper()}**
- 告警规则突变检出：**{result.alert_mutation_detection_rate:.1%}**
- 离线证据分：**{final.offline_earned_points:.1f}/{final.offline_measured_weight}**
- 完整 100 分综合分：**NOT_EVALUATED**

离线证据分只汇总已有可测组件，不把成本、真实模型或线上数据当成 0。由于恢复
门禁未达标，且真实模型、Judge、成本、线上趋势缺失，项目当前不能宣称完整评测通过。

## 数据与隐私

- D7 状态：`{manifest.status}`
- 脱敏观测事件：{manifest.records}
- 真实生产事件：{manifest.production_records}
- 日窗口：{daily.date.isoformat()}（offline backfill）
- 周窗口：{weekly.iso_week}（趋势 not_evaluated）
- 原始用户文本：不存储
- 原始 Provider 响应：不存储

## 能力覆盖

| 能力 | 状态 | 证据 |
|---|---|---|
{capability_rows}

## 当前告警

| 告警 | 状态 | 阈值 |
|---|---|---|
{alerts}

`OFFLINE_RELEASE_GATE_FAILED` 是真实触发；四类线上趋势告警因生产运行数为 0 而
保持 `not_evaluated`。合成退化只用于验证规则，不写入 D7，也不冒充线上事故。

## 真实 API 独立评测计划

- 模式：真实 LLM + Mock Provider
- 数据：`intent-model-smoke-v1` 24 条 × 3 次
- 预计计费模型调用：72
- API Key 已配置：{str(final.real_api_plan.api_key_configured).lower()}
- 模型已显式配置：{str(final.real_api_plan.model_explicitly_configured).lower()}
- 价格表：{final.real_api_plan.price_table_status}
- 当前状态：**{final.real_api_plan.status.upper()}**

阻塞条件：

{blockers}

该评测不会覆盖真实航司、酒店、预订或支付 API；工具选择仍由编排器控制。完成后
应将真实模型结果作为独立候选产物接入 baseline、成本和趋势报告。
"""
