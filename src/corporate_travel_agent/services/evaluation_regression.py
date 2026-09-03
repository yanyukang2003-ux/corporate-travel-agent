"""评测什么：回归——坏例集构建、基线冻结与相对基线的指标退化检测。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent import __version__
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_quality import EvaluationResult

SHA256_PATTERN = r"^[a-f0-9]{64}$"
REGRESSION_EVALUATOR_VERSION: Final = "regression-evaluator-v1"


class RegressionEvaluationError(RuntimeError):
    """回归评测失败。"""
    pass


class RegressionModel(BaseModel):
    """回归评测模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class ManualBadCaseIntake(RegressionModel):
    """人工录入坏例的摄入记录。"""
    source_case_id: str = Field(min_length=1)
    source_type: Literal["test_failure", "user_correction", "manual_review"]
    failure_type: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    fixture_reference: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    minimal_reproducer: dict[str, Any]
    review_status: Literal["pending", "accepted", "rejected"]
    review_note: str = Field(min_length=1)


class BadCaseRecord(RegressionModel):
    """坏例库中的正式记录。"""
    schema_version: Literal[1] = 1
    bad_case_id: str = Field(min_length=1)
    source_case_id: str = Field(min_length=1)
    source_type: Literal[
        "evaluation_failure", "test_failure", "user_correction", "manual_review"
    ]
    source_evaluation_id: str | None
    source_artifact: str = Field(min_length=1)
    failure_type: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high", "critical"]
    fixture_reference: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    input_hash: str = Field(pattern=SHA256_PATTERN)
    failure_signature: str = Field(pattern=SHA256_PATTERN)
    trace_pattern_hash: str = Field(pattern=SHA256_PATTERN)
    deduplication_key: str = Field(pattern=SHA256_PATTERN)
    review_status: Literal["pending", "accepted", "rejected", "duplicate"]
    review_basis: str = Field(min_length=1)
    regression_eligible: bool
    redaction_status: Literal["synthetic_no_sensitive_data", "redacted"]


class ArtifactReference(RegressionModel):
    """产物文件引用（路径+哈希）。"""
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    evaluation_id: str | None = None
    dataset_id: str | None = None
    dataset_version: str | None = None
    selected_cases: int | None = Field(default=None, ge=0)
    expected_runs: int | None = Field(default=None, ge=0)


class BadCaseDatasetManifest(RegressionModel):
    """坏例数据集清单。"""
    schema_version: Literal[1] = 1
    dataset_id: Literal["bad-case-regression"] = "bad-case-regression"
    version: str = Field(min_length=1)
    status: Literal["frozen"] = "frozen"
    generated_at: datetime
    records: int = Field(ge=0)
    candidate_records: int = Field(ge=0)
    review_status_counts: dict[str, int]
    source_type_counts: dict[str, int]
    cases_file: Literal["cases.jsonl"] = "cases.jsonl"
    cases_sha256: str = Field(pattern=SHA256_PATTERN)
    candidates_file: Literal["candidates.jsonl"] = "candidates.jsonl"
    candidates_sha256: str = Field(pattern=SHA256_PATTERN)
    source_artifacts: dict[str, ArtifactReference]
    deduplication_fields: tuple[str, ...]
    limitations: tuple[str, ...]


class BaselineMetric(RegressionModel):
    """基线中的单项指标。"""
    source_stage: str
    source_metric: str
    status: Literal["measured", "unavailable", "not_applicable"]
    value: float | None
    unit: str | None
    direction: Literal["min", "max"]
    absolute_threshold: float | None
    regression_tolerance: float = Field(ge=0)
    required_for_release: bool
    note: str | None = None


class RegressionBaseline(RegressionModel):
    """冻结的回归基线。"""
    schema_version: Literal[1] = 1
    baseline_id: str = Field(min_length=1)
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    status: Literal["frozen"] = "frozen"
    frozen_at: datetime
    project_version: str
    code_revision: str | None
    source_artifacts: dict[str, ArtifactReference]
    metrics: dict[str, BaselineMetric]
    limitations: tuple[str, ...]


GateStatus = Literal["pass", "fail", "not_evaluated"]


class MetricComparison(RegressionModel):
    """当前运行相对基线的指标对比。"""
    metric_id: str
    baseline_status: Literal["measured", "unavailable", "not_applicable"]
    baseline_value: float | None
    current_status: Literal["measured", "unavailable", "not_applicable"]
    current_value: float | None
    absolute_threshold: float | None
    direction: Literal["min", "max"]
    relative_regression: float | None
    allowed_regression: float
    absolute_gate: GateStatus
    regression_gate: GateStatus
    evidence: tuple[str, ...]


class MutationCheck(RegressionModel):
    """回归评测突变检查。"""
    mutation_id: str
    metric_id: str
    expected_gate: GateStatus
    actual_gate: GateStatus
    detected: bool


class RegressionRunSummary(RegressionModel):
    """回归评测运行汇总。"""
    schema_version: Literal[1] = 1
    evaluation_id: str
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    evaluator_version: Literal["regression-evaluator-v1"] = REGRESSION_EVALUATOR_VERSION
    generated_at: datetime
    project_version: str
    comparison_mode: Literal["baseline_initialization_self_check", "candidate_regression"]
    independent_candidate_run: bool
    baseline_file: str
    baseline_sha256: str = Field(pattern=SHA256_PATTERN)
    current_artifacts: dict[str, ArtifactReference]
    comparisons: tuple[MetricComparison, ...]
    absolute_gate_status: GateStatus
    regression_gate_status: GateStatus
    release_gate_status: GateStatus
    unevaluated_required_metrics: tuple[str, ...]
    failed_absolute_metrics: tuple[str, ...]
    regressed_metrics: tuple[str, ...]
    mutation_checks: tuple[MutationCheck, ...]
    mutation_detection_rate: float = Field(ge=0, le=1)
    limitations: tuple[str, ...]


_METRIC_SPECS: dict[str, tuple[str, str, str, float | None, float, bool, str | None]] = {
    "quality.task_pass_rate": ("quality", "task_pass_rate", "min", 1.0, 0.0, True, None),
    "quality.hard_assertion_pass_rate": (
        "quality",
        "hard_assertion_pass_rate",
        "min",
        1.0,
        0.0,
        True,
        None,
    ),
    "quality.judge_quality_score": (
        "quality",
        "judge_quality_score",
        "min",
        4.0,
        0.0,
        True,
        "No calibrated Judge exposure in the deterministic baseline.",
    ),
    "trajectory.trajectory_pass_rate": (
        "trajectory",
        "trajectory_pass_rate",
        "min",
        1.0,
        0.0,
        True,
        None,
    ),
    "trajectory.tool_hallucination_call_rate": (
        "trajectory",
        "tool_hallucination_call_rate",
        "max",
        0.0,
        0.0,
        True,
        None,
    ),
    "trajectory.parameter_hallucination_call_rate": (
        "trajectory",
        "parameter_hallucination_call_rate",
        "max",
        0.0,
        0.0,
        True,
        None,
    ),
    "trajectory.shadow_hallucination_claim_rate": (
        "trajectory",
        "shadow_hallucination_claim_rate",
        "max",
        0.0,
        0.0,
        True,
        None,
    ),
    "efficiency.normal_duplicate_call_rate": (
        "efficiency",
        "normal_duplicate_call_rate",
        "max",
        0.0,
        0.0,
        True,
        None,
    ),
    "efficiency.redundant_call_rate": (
        "efficiency",
        "redundant_call_rate",
        "max",
        0.1,
        0.0,
        True,
        None,
    ),
    "efficiency.tool_calls_p95": (
        "efficiency",
        "tool_calls_per_passed_task_p95",
        "max",
        7.0,
        0.0,
        True,
        None,
    ),
    "efficiency.tool_calls_max": (
        "efficiency",
        "tool_calls_per_passed_task_max",
        "max",
        12.0,
        0.0,
        True,
        None,
    ),
    "recovery.recoverable_fault_success_rate": (
        "recovery",
        "recoverable_fault_success_rate",
        "min",
        0.8,
        0.0,
        True,
        "Known Stage 5 gap remains visible; a baseline never waives the absolute gate.",
    ),
    "recovery.safe_degradation_rate": (
        "recovery",
        "safe_degradation_rate",
        "min",
        0.95,
        0.0,
        True,
        None,
    ),
    "recovery.unsafe_recovery_rate": (
        "recovery",
        "unsafe_recovery_rate",
        "max",
        0.0,
        0.0,
        True,
        None,
    ),
    "recovery.partial_result_disclosure_rate": (
        "recovery",
        "partial_result_disclosure_rate",
        "min",
        1.0,
        0.0,
        True,
        "Project-specific completeness gate for partial provider coverage.",
    ),
    "stability.pass_power_3": (
        "performance",
        "pass_power_3",
        "min",
        1.0,
        0.0,
        True,
        "Absolute 100% target applies to deterministic D1; real-model target is 80%.",
    ),
    "stability.mixed_run_rate": (
        "performance",
        "mixed_run_rate",
        "max",
        0.1,
        0.0,
        True,
        None,
    ),
    "performance.total_latency_ms_p95": (
        "performance",
        "total_latency_ms_p95",
        "max",
        None,
        0.5,
        False,
        "Sub-millisecond local Mock baseline; use as a regression signal, not an SLO.",
    ),
    "cost.per_successful_task": (
        "performance",
        "estimated_cost_per_successful_task",
        "max",
        None,
        0.2,
        False,
        "Enabled only when both baseline and candidate have measured cost.",
    ),
    "stability.real_model_gate": (
        "performance",
        "real_model_stability_gate",
        "min",
        1.0,
        0.0,
        True,
        "Unavailable until the explicit billable 24 x 3 model run is approved.",
    ),
}


def build_bad_case_dataset(
    *,
    phase5_result_path: str | Path,
    fault_cases_path: str | Path,
    manual_intake_path: str | Path,
    output_directory: str | Path,
    version: str = "1.0.0",
) -> BadCaseDatasetManifest:
    """从评测结果与人工录入构建坏例数据集。"""
    result_path = Path(phase5_result_path).expanduser().resolve()
    fault_path = Path(fault_cases_path).expanduser().resolve()
    manual_path = Path(manual_intake_path).expanduser().resolve()
    result = EvaluationResult.model_validate_json(result_path.read_bytes())
    fault_cases = {
        item["case_id"]: item for item in _load_jsonl_objects(fault_path)
    }
    candidate_signatures = {
        item.case_id: item.failure_signature for item in result.bad_case_candidates
    }
    candidates: list[BadCaseRecord] = []
    for failure in result.hard_failures:
        fixture = fault_cases.get(failure.case_id)
        if fixture is None:
            raise RegressionEvaluationError(
                f"missing frozen fault fixture for {failure.case_id}"
            )
        expected_behavior = _expected_behavior_for_failure(failure.failure_type)
        trace_pattern = {
            "failure_type": failure.failure_type,
            "fault_type": fixture["fault_type"],
            "injection_point": fixture["injection_point"],
            "evidence": sorted(failure.evidence),
        }
        signature = candidate_signatures.get(failure.case_id)
        if signature is None:
            raise RegressionEvaluationError(
                f"missing bad-case candidate signature for {failure.case_id}"
            )
        candidates.append(
            BadCaseRecord(
                bad_case_id=f"d6-{stable_hash([failure.case_id, signature])[:16]}",
                source_case_id=failure.case_id,
                source_type="evaluation_failure",
                source_evaluation_id=result.evaluation_id,
                source_artifact=_portable_path(result_path),
                failure_type=failure.failure_type,
                risk_level="high",
                fixture_reference=(
                    f"{_portable_path(fault_path)}#case_id={failure.case_id}"
                ),
                expected_behavior=expected_behavior,
                input_hash=stable_hash(fixture),
                failure_signature=signature,
                trace_pattern_hash=stable_hash(trace_pattern),
                deduplication_key=stable_hash(
                    [failure.case_id, failure.failure_type, expected_behavior]
                ),
                review_status="accepted",
                review_basis=(
                    "pre-run expected behavior in frozen D5 fixture and rule evaluator; "
                    "not generated from model output"
                ),
                regression_eligible=True,
                redaction_status="synthetic_no_sensitive_data",
            )
        )

    for payload in _load_jsonl_objects(manual_path):
        item = ManualBadCaseIntake.model_validate(payload)
        signature = stable_hash(
            [item.failure_type, item.fixture_reference, item.minimal_reproducer]
        )
        candidates.append(
            BadCaseRecord(
                bad_case_id=f"d6-{stable_hash([item.source_case_id, signature])[:16]}",
                source_case_id=item.source_case_id,
                source_type=item.source_type,
                source_evaluation_id=None,
                source_artifact=_portable_path(manual_path),
                failure_type=item.failure_type,
                risk_level=item.risk_level,
                fixture_reference=item.fixture_reference,
                expected_behavior=item.expected_behavior,
                input_hash=stable_hash(item.minimal_reproducer),
                failure_signature=signature,
                trace_pattern_hash=stable_hash(
                    [item.failure_type, item.fixture_reference]
                ),
                deduplication_key=stable_hash(
                    [item.fixture_reference, item.failure_type, item.expected_behavior]
                ),
                review_status=item.review_status,
                review_basis=item.review_note,
                regression_eligible=item.review_status == "accepted",
                redaction_status="synthetic_no_sensitive_data",
            )
        )

    candidates = _deduplicate_bad_cases(candidates)
    accepted = tuple(item for item in candidates if item.regression_eligible)
    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise RegressionEvaluationError(f"output directory already exists: {output_root}")
    output_root.mkdir(parents=True)
    candidates_payload = _jsonl_payload(candidates)
    accepted_payload = _jsonl_payload(accepted)
    (output_root / "candidates.jsonl").write_bytes(candidates_payload)
    (output_root / "cases.jsonl").write_bytes(accepted_payload)
    manifest = BadCaseDatasetManifest(
        version=version,
        generated_at=datetime.now(UTC),
        records=len(accepted),
        candidate_records=len(candidates),
        review_status_counts=dict(Counter(item.review_status for item in candidates)),
        source_type_counts=dict(Counter(item.source_type for item in candidates)),
        cases_sha256=_sha256(accepted_payload),
        candidates_sha256=_sha256(candidates_payload),
        source_artifacts={
            "phase5_result": _artifact_reference(result_path),
            "fault_cases": _artifact_reference(fault_path),
            "manual_intake": _artifact_reference(manual_path),
        },
        deduplication_fields=(
            "input_hash",
            "failure_signature",
            "trace_pattern_hash",
            "deduplication_key",
        ),
        limitations=(
            "D6 v1 contains synthetic fixtures only; production inputs are not copied.",
            "The authentication candidate remains pending and is excluded from release gates.",
            "Accepted D5 cases rely on expectations frozen before execution, not model labels.",
        ),
    )
    (output_root / "manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return manifest


def freeze_regression_baseline(
    *,
    artifact_paths: dict[str, str | Path],
    output_path: str | Path,
    baseline_id: str = "agent-eval-baseline-v1",
) -> RegressionBaseline:
    """冻结当前评测结果为回归基线。"""
    resolved, results = _load_stage_results(artifact_paths)
    metrics = _extract_metrics(results)
    baseline = RegressionBaseline(
        baseline_id=baseline_id,
        frozen_at=datetime.now(UTC),
        project_version=__version__,
        code_revision=None,
        source_artifacts={
            key: _artifact_reference(path, results[key]) for key, path in resolved.items()
        },
        metrics=metrics,
        limitations=(
            "Code revision is unavailable because the project is not a Git worktree.",
            "Latency is a local deterministic Mock measurement and is not a production SLO.",
            "Token and cost remain null until a measured model run and configured "
            "price table exist.",
            "The frozen baseline records known failures; it does not waive absolute gates.",
        ),
    )
    target = Path(output_path).expanduser().resolve()
    if target.exists():
        raise RegressionEvaluationError(f"baseline already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
    return baseline


def evaluate_regression(
    *,
    baseline_path: str | Path,
    current_artifact_paths: dict[str, str | Path],
    output_directory: str | Path,
    comparison_mode: Literal[
        "baseline_initialization_self_check", "candidate_regression"
    ] = "candidate_regression",
) -> RegressionRunSummary:
    """对照基线评估是否发生回归。"""
    baseline_file = Path(baseline_path).expanduser().resolve()
    baseline_payload = baseline_file.read_bytes()
    baseline = RegressionBaseline.model_validate_json(baseline_payload)
    resolved, results = _load_stage_results(current_artifact_paths)
    current_references = {
        key: _artifact_reference(path, results[key]) for key, path in resolved.items()
    }
    if comparison_mode == "baseline_initialization_self_check":
        mismatched = tuple(
            key
            for key, reference in current_references.items()
            if baseline.source_artifacts.get(key) != reference
        )
        if mismatched:
            raise RegressionEvaluationError(
                "initialization self-check artifacts differ from the frozen baseline: "
                f"{', '.join(mismatched)}"
            )
    else:
        _validate_candidate_coverage(baseline.source_artifacts, current_references)
    current = _extract_metrics(results)
    comparisons = tuple(
        compare_metric(metric_id, metric, current[metric_id])
        for metric_id, metric in baseline.metrics.items()
    )
    mutation_checks = _run_mutation_checks(baseline)
    failed_absolute = tuple(
        item.metric_id for item in comparisons if item.absolute_gate == "fail"
    )
    regressed = tuple(
        item.metric_id for item in comparisons if item.regression_gate == "fail"
    )
    unevaluated_required = tuple(
        item.metric_id
        for item in comparisons
        if baseline.metrics[item.metric_id].required_for_release
        and item.absolute_gate == "not_evaluated"
    )
    absolute_status = _aggregate_gate(item.absolute_gate for item in comparisons)
    regression_status = _aggregate_gate(item.regression_gate for item in comparisons)
    release_status: GateStatus = (
        "pass"
        if absolute_status == "pass"
        and regression_status == "pass"
        and not unevaluated_required
        else "fail"
    )
    summary = RegressionRunSummary(
        evaluation_id=f"eval-{uuid4()}",
        generated_at=datetime.now(UTC),
        project_version=__version__,
        comparison_mode=comparison_mode,
        independent_candidate_run=comparison_mode == "candidate_regression",
        baseline_file=_portable_path(baseline_file),
        baseline_sha256=_sha256(baseline_payload),
        current_artifacts=current_references,
        comparisons=comparisons,
        absolute_gate_status=absolute_status,
        regression_gate_status=regression_status,
        release_gate_status=release_status,
        unevaluated_required_metrics=unevaluated_required,
        failed_absolute_metrics=failed_absolute,
        regressed_metrics=regressed,
        mutation_checks=mutation_checks,
        mutation_detection_rate=(
            sum(item.detected for item in mutation_checks) / len(mutation_checks)
        ),
        limitations=(
            "This initialization run reuses the baseline artifacts and is not an "
            "independent candidate run."
            if comparison_mode == "baseline_initialization_self_check"
            else "Candidate artifacts must come from a separately executed evaluation run.",
            "Unknown Judge, real-model, Token, or cost values are never replaced with zero.",
        ),
    )
    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise RegressionEvaluationError(f"output directory already exists: {output_root}")
    output_root.mkdir(parents=True)
    result_path = output_root / "regression-evaluation-result.json"
    result_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    (output_root / "mutation-checks.json").write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in mutation_checks],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_root / "evaluation-report.md").write_text(
        _render_regression_report(summary), encoding="utf-8"
    )
    return summary


def compare_metric(
    metric_id: str,
    baseline: BaselineMetric,
    current: BaselineMetric,
) -> MetricComparison:
    """比较单项指标相对基线的变化。"""
    evidence: list[str] = []
    if baseline.status != "measured" or current.status != "measured":
        evidence.append(
            "Both baseline and candidate must be measured; zero substitution is forbidden."
        )
        return MetricComparison(
            metric_id=metric_id,
            baseline_status=baseline.status,
            baseline_value=baseline.value,
            current_status=current.status,
            current_value=current.value,
            absolute_threshold=baseline.absolute_threshold,
            direction=baseline.direction,
            relative_regression=None,
            allowed_regression=baseline.regression_tolerance,
            absolute_gate="not_evaluated",
            regression_gate="not_evaluated",
            evidence=tuple(evidence),
        )
    if baseline.value is None or current.value is None:
        raise RegressionEvaluationError(f"measured metric {metric_id} has no value")
    absolute_gate: GateStatus = "not_evaluated"
    if baseline.absolute_threshold is not None:
        absolute_gate = (
            "pass"
            if _meets_threshold(
                current.value, baseline.absolute_threshold, baseline.direction
            )
            else "fail"
        )
    relative = _relative_regression(
        baseline.value, current.value, baseline.direction
    )
    regression_gate: GateStatus = (
        "pass" if relative <= baseline.regression_tolerance + 1e-12 else "fail"
    )
    evidence.extend(
        (
            f"baseline={baseline.value}",
            f"current={current.value}",
            f"relative_regression={relative}",
        )
    )
    return MetricComparison(
        metric_id=metric_id,
        baseline_status=baseline.status,
        baseline_value=baseline.value,
        current_status=current.status,
        current_value=current.value,
        absolute_threshold=baseline.absolute_threshold,
        direction=baseline.direction,
        relative_regression=relative,
        allowed_regression=baseline.regression_tolerance,
        absolute_gate=absolute_gate,
        regression_gate=regression_gate,
        evidence=tuple(evidence),
    )


def _load_stage_results(
    paths: dict[str, str | Path],
) -> tuple[dict[str, Path], dict[str, EvaluationResult]]:
    required = {spec[0] for spec in _METRIC_SPECS.values()}
    if set(paths) != required:
        raise RegressionEvaluationError(
            f"artifact keys must be exactly {sorted(required)}, got {sorted(paths)}"
        )
    resolved = {key: Path(value).expanduser().resolve() for key, value in paths.items()}
    results = {
        key: EvaluationResult.model_validate_json(path.read_bytes())
        for key, path in resolved.items()
    }
    return resolved, results


def _extract_metrics(results: dict[str, EvaluationResult]) -> dict[str, BaselineMetric]:
    extracted: dict[str, BaselineMetric] = {}
    for metric_id, spec in _METRIC_SPECS.items():
        stage, source_metric, direction, threshold, tolerance, required, note = spec
        source = results[stage].metrics.get(source_metric)
        if source is None:
            raise RegressionEvaluationError(f"{stage} artifact has no {source_metric} metric")
        extracted[metric_id] = BaselineMetric(
            source_stage=stage,
            source_metric=source_metric,
            status=source.status,
            value=source.value,
            unit=source.unit,
            direction=cast(Literal["min", "max"], direction),
            absolute_threshold=threshold,
            regression_tolerance=tolerance,
            required_for_release=required,
            note=note,
        )
    return extracted


def _run_mutation_checks(baseline: RegressionBaseline) -> tuple[MutationCheck, ...]:
    mutations = (
        ("quality_drop", "quality.task_pass_rate", 0.99, "fail", "absolute_gate"),
        (
            "tool_hallucination",
            "trajectory.tool_hallucination_call_rate",
            0.01,
            "fail",
            "absolute_gate",
        ),
        (
            "duplicate_call",
            "efficiency.normal_duplicate_call_rate",
            0.01,
            "fail",
            "absolute_gate",
        ),
        (
            "recovery_drop",
            "recovery.recoverable_fault_success_rate",
            0.3,
            "fail",
            "regression_gate",
        ),
        (
            "latency_plus_51_percent",
            "performance.total_latency_ms_p95",
            (baseline.metrics["performance.total_latency_ms_p95"].value or 0.0) * 1.51,
            "fail",
            "regression_gate",
        ),
    )
    checks: list[MutationCheck] = []
    for mutation_id, metric_id, value, expected, gate_field in mutations:
        base = baseline.metrics[metric_id]
        current = base.model_copy(update={"status": "measured", "value": value})
        comparison = compare_metric(metric_id, base, current)
        actual = getattr(comparison, gate_field)
        checks.append(
            MutationCheck(
                mutation_id=mutation_id,
                metric_id=metric_id,
                expected_gate=cast(GateStatus, expected),
                actual_gate=actual,
                detected=actual == expected,
            )
        )
    cost = baseline.metrics["cost.per_successful_task"]
    cost_comparison = compare_metric("cost.per_successful_task", cost, cost)
    checks.append(
        MutationCheck(
            mutation_id="unknown_cost_not_zero",
            metric_id="cost.per_successful_task",
            expected_gate="not_evaluated",
            actual_gate=cost_comparison.regression_gate,
            detected=cost_comparison.regression_gate == "not_evaluated",
        )
    )
    return tuple(checks)


def _expected_behavior_for_failure(failure_type: str) -> str:
    expectations = {
        "AUTONOMOUS_RECOVERY_FAILED": (
            "Retry a one-shot recoverable provider fault within the bounded retry policy, "
            "or fail closed with an explicit user-visible limitation."
        ),
        "PARTIAL_RESULT_NOT_DISCLOSED": (
            "Disclose incomplete provider coverage in the user-visible result and avoid "
            "presenting partial inventory as complete."
        ),
    }
    try:
        return expectations[failure_type]
    except KeyError as exc:
        raise RegressionEvaluationError(
            f"no reviewed expectation for failure type {failure_type}"
        ) from exc


def _deduplicate_bad_cases(records: list[BadCaseRecord]) -> list[BadCaseRecord]:
    seen: dict[str, str] = {}
    output: list[BadCaseRecord] = []
    for item in records:
        existing = seen.get(item.deduplication_key)
        if existing is None:
            seen[item.deduplication_key] = item.bad_case_id
            output.append(item)
            continue
        output.append(
            item.model_copy(
                update={
                    "review_status": "duplicate",
                    "review_basis": f"duplicate_of:{existing}",
                    "regression_eligible": False,
                }
            )
        )
    return output


def _aggregate_gate(statuses: Any) -> GateStatus:
    values = tuple(statuses)
    if "fail" in values:
        return "fail"
    if "pass" in values:
        return "pass"
    return "not_evaluated"


def _meets_threshold(value: float, threshold: float, direction: str) -> bool:
    return value >= threshold - 1e-12 if direction == "min" else value <= threshold + 1e-12


def _relative_regression(baseline: float, current: float, direction: str) -> float:
    deterioration = baseline - current if direction == "min" else current - baseline
    if deterioration <= 0:
        return 0.0
    if math.isclose(baseline, 0.0, abs_tol=1e-15):
        return math.inf
    return deterioration / abs(baseline)


def _validate_candidate_coverage(
    baseline: dict[str, ArtifactReference],
    current: dict[str, ArtifactReference],
) -> None:
    fields = ("dataset_id", "dataset_version", "selected_cases", "expected_runs")
    mismatches: list[str] = []
    for stage, baseline_reference in baseline.items():
        current_reference = current[stage]
        for field in fields:
            if getattr(baseline_reference, field) != getattr(current_reference, field):
                mismatches.append(
                    f"{stage}.{field}: baseline={getattr(baseline_reference, field)!r}, "
                    f"current={getattr(current_reference, field)!r}"
                )
    if mismatches:
        raise RegressionEvaluationError(
            "candidate coverage does not match the frozen baseline: " + "; ".join(mismatches)
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


def _load_jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    return tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def _jsonl_payload(records: Any) -> bytes:
    return "".join(f"{item.model_dump_json()}\n" for item in records).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _render_regression_report(summary: RegressionRunSummary) -> str:
    rows = "\n".join(
        f"| {item.metric_id} | {item.baseline_value} | {item.current_value} | "
        f"{item.absolute_gate} | {item.regression_gate} |"
        for item in summary.comparisons
    )
    return f"""# Stage 7 Baseline and Regression Evaluation

- Evaluation ID: `{summary.evaluation_id}`
- Mode: `{summary.comparison_mode}`
- Independent candidate run: `{str(summary.independent_candidate_run).lower()}`
- Absolute gate: **{summary.absolute_gate_status.upper()}**
- Regression gate: **{summary.regression_gate_status.upper()}**
- Release gate: **{summary.release_gate_status.upper()}**
- Mutation detection: `{summary.mutation_detection_rate:.1%}`

The initialization comparison intentionally reuses the frozen source artifacts. It verifies
extraction, hashing, comparison and gate behavior; it is not evidence from a new Agent run.

## Metric comparison

| Metric | Baseline | Current | Absolute | Regression |
|---|---:|---:|---|---|
{rows}

## Known absolute failures

{', '.join(summary.failed_absolute_metrics) or 'None'}

## Required but unevaluated

{', '.join(summary.unevaluated_required_metrics) or 'None'}

## Regressions

{', '.join(summary.regressed_metrics) or 'None'}

## Interpretation

No regression against the initialization snapshot does not mean the release gate passes.
Known Stage 5 recovery gaps remain absolute failures. Judge and real-model stability remain
unevaluated. Token and cost remain null because no LLM was exposed in the measured baseline.
"""
