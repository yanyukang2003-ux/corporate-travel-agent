"""评测什么：性能与稳定性——延迟分布、资源消耗、成本与跨次一致性。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corporate_travel_agent import __version__
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_quality import (
    BadCaseCandidate,
    CostSummary,
    EvaluationCoverage,
    EvaluationResult,
    HardFailure,
    MetricResult,
)
from corporate_travel_agent.services.evaluation_runner import (
    CaseRunSummary,
    EvaluationRunSummary,
)
from corporate_travel_agent.services.evaluation_trace import EvaluationTrace

PERFORMANCE_EVALUATOR_VERSION: Final = "performance-stability-evaluator-v1"
GateStatus = Literal["pass", "fail", "not_evaluated"]
SHA256_PATTERN = r"^[a-f0-9]{64}$"


class PerformanceEvaluationError(RuntimeError):
    """性能/稳定性评测失败。"""
    pass


class PerformanceModel(BaseModel):
    """性能评测模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelTokenPrice(PerformanceModel):
    """单模型 token 单价。"""
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cached_input: float | None = Field(default=None, ge=0)
    cache_write_input: float | None = Field(default=None, ge=0)


class ModelPriceTable(PerformanceModel):
    """模型价目表。"""
    schema_version: Literal[1]
    price_table_version: str = Field(min_length=1)
    status: Literal["configured", "unconfigured"]
    currency: str = Field(min_length=1)
    unit: Literal["per_1m_tokens"]
    effective_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source: str = Field(min_length=1)
    models: dict[str, ModelTokenPrice]

    @model_validator(mode="after")
    def configured_table_has_prices(self) -> ModelPriceTable:
        if self.status == "configured" and not self.models:
            raise ValueError("a configured price table must contain at least one model")
        if self.status == "unconfigured" and self.models:
            raise ValueError("an unconfigured price table cannot contain model prices")
        return self


class Distribution(PerformanceModel):
    """数值分布摘要（分位数等）。"""
    count: int = Field(ge=1)
    total: float = Field(ge=0)
    mean: float = Field(ge=0)
    p50: float = Field(ge=0)
    p95: float = Field(ge=0)
    maximum: float = Field(ge=0)
    unit: str


class ResourceRunEvaluation(PerformanceModel):
    """单次运行资源消耗评估。"""
    run_id: str
    case_id: str
    attempt: int = Field(ge=1)
    passed: bool
    total_latency_ms: float = Field(ge=0)
    recorded_step_latency_ms: float = Field(ge=0)
    tool_latency_ms: float = Field(ge=0)
    llm_latency_ms: float = Field(ge=0)
    external_calls: int = Field(ge=0)
    provider_calls: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_usage_complete: bool | None
    estimated_cost: float | None = Field(default=None, ge=0)
    outcome_signature: str = Field(pattern=SHA256_PATTERN)
    trajectory_signature: str = Field(pattern=SHA256_PATTERN)


class StabilityCaseEvaluation(PerformanceModel):
    """稳定性单用例评估。"""
    case_id: str
    attempts: int = Field(ge=1)
    passed_attempts: int = Field(ge=0)
    all_passed: bool
    any_passed: bool
    mixed_pass_result: bool
    output_consistent: bool
    trajectory_consistent: bool
    run_ids: tuple[str, ...]


class PerformanceStabilityRunSummary(PerformanceModel):
    """性能与稳定性运行汇总。"""
    schema_version: Literal[1] = 1
    evaluation_id: str
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    evaluator_version: Literal["performance-stability-evaluator-v1"] = (
        PERFORMANCE_EVALUATOR_VERSION
    )
    project_version: str
    generated_at: datetime
    evaluation_mode: str
    dataset_id: str
    dataset_version: str
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    source_run_summary_file: str
    source_run_summary_sha256: str = Field(pattern=SHA256_PATTERN)
    source_traces_file: str
    source_traces_sha256: str = Field(pattern=SHA256_PATTERN)
    selected_cases: int = Field(ge=1)
    attempts_per_case: int = Field(ge=1)
    completed_runs: int = Field(ge=0)
    passed_runs: int = Field(ge=0)
    total_external_calls: int = Field(ge=0)
    calls_by_kind: dict[str, int]
    total_latency: Distribution
    tool_latency: Distribution
    external_calls_per_run: Distribution
    input_tokens_status: Literal["measured", "unavailable", "not_applicable"]
    output_tokens_status: Literal["measured", "unavailable", "not_applicable"]
    cost_status: Literal["measured", "unavailable", "not_applicable"]
    price_table_version: str | None
    price_table_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    performance_run_results_file: str
    performance_run_results_sha256: str = Field(pattern=SHA256_PATTERN)
    stability_case_results_file: str
    stability_case_results_sha256: str = Field(pattern=SHA256_PATTERN)
    evaluation_result_file: str
    evaluation_result_sha256: str = Field(pattern=SHA256_PATTERN)
    pass_at_1: float = Field(ge=0, le=1)
    pass_power_3: float | None = Field(default=None, ge=0, le=1)
    pass_at_3: float | None = Field(default=None, ge=0, le=1)
    mixed_run_rate: float | None = Field(default=None, ge=0, le=1)
    output_consistency_rate: float | None = Field(default=None, ge=0, le=1)
    trajectory_consistency_rate: float | None = Field(default=None, ge=0, le=1)
    deterministic_stability_gate: Literal["pass", "fail", "not_evaluated"]
    real_model_stability_gate: Literal["pass", "fail", "not_evaluated"]
    cost_regression_gate: Literal["pass", "fail", "not_evaluated"]
    latency_regression_gate: Literal["pass", "fail", "not_evaluated"]
    stage_6_pipeline_passed: bool
    full_protocol_gate: Literal["not_evaluated"] = "not_evaluated"
    real_model_calls: int = Field(ge=0)
    limitations: tuple[str, ...]


def load_model_price_table(path: str | Path) -> tuple[ModelPriceTable, str]:
    """加载模型 token 价目表。"""
    payload = Path(path).expanduser().resolve().read_bytes()
    table = ModelPriceTable.model_validate_json(payload)
    return table, _sha256(payload)


def evaluate_performance_and_stability(
    *,
    source_run_directory: str | Path,
    output_directory: str | Path,
    price_table_path: str | Path | None = None,
    expected_attempts: int = 3,
) -> PerformanceStabilityRunSummary:
    """评估运行的性能与稳定性指标。"""
    if expected_attempts < 1:
        raise PerformanceEvaluationError("expected_attempts must be at least one")
    source_root = Path(source_run_directory).expanduser().resolve()
    summary_path = source_root / "run-summary.json"
    if not summary_path.is_file():
        raise PerformanceEvaluationError(f"source run summary does not exist: {summary_path}")
    source_summary_payload = summary_path.read_bytes()
    source_summary = EvaluationRunSummary.model_validate_json(source_summary_payload)
    trace_path = source_root / source_summary.traces_file
    trace_payload = trace_path.read_bytes()
    if _sha256(trace_payload) != source_summary.traces_sha256:
        raise PerformanceEvaluationError("source trace hash does not match run summary")
    traces = tuple(
        EvaluationTrace.model_validate_json(line)
        for line in trace_payload.splitlines()
        if line.strip()
    )
    if len(traces) != source_summary.completed_runs:
        raise PerformanceEvaluationError("source trace count does not match run summary")
    if source_summary.attempts_per_case != expected_attempts:
        raise PerformanceEvaluationError(
            f"expected {expected_attempts} attempts per case, got "
            f"{source_summary.attempts_per_case}"
        )
    if source_summary.completed_runs != source_summary.expected_runs:
        raise PerformanceEvaluationError("source run is incomplete")

    price_table: ModelPriceTable | None = None
    price_table_sha256: str | None = None
    if price_table_path is not None:
        price_table, price_table_sha256 = load_model_price_table(price_table_path)

    summary_by_run = {item.run_id: item for item in source_summary.runs}
    if len(summary_by_run) != len(source_summary.runs):
        raise PerformanceEvaluationError("source run IDs are not unique")
    if {trace.run_id for trace in traces} != set(summary_by_run):
        raise PerformanceEvaluationError("trace and run-summary IDs do not match")
    _validate_attempt_matrix(
        traces,
        selected_cases=source_summary.selected_cases,
        expected_attempts=expected_attempts,
    )

    run_evaluations = tuple(
        _evaluate_resource_run(trace, summary_by_run[trace.run_id], price_table)
        for trace in traces
    )
    case_evaluations = _evaluate_stability_cases(run_evaluations)
    evaluation_id = f"eval-{uuid4()}"
    formal_result = _build_evaluation_result(
        evaluation_id=evaluation_id,
        source_summary=source_summary,
        run_evaluations=run_evaluations,
        case_evaluations=case_evaluations,
        price_table=price_table,
    )

    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise PerformanceEvaluationError(f"output directory already exists: {output_root}")
    output_root.mkdir(parents=True)
    run_results_path = output_root / "performance-run-results.jsonl"
    run_results_payload = "".join(
        f"{item.model_dump_json()}\n" for item in run_evaluations
    ).encode()
    run_results_path.write_bytes(run_results_payload)
    case_results_path = output_root / "stability-case-results.jsonl"
    case_results_payload = "".join(
        f"{item.model_dump_json()}\n" for item in case_evaluations
    ).encode()
    case_results_path.write_bytes(case_results_payload)
    result_path = output_root / "performance-stability-evaluation-result.json"
    result_payload = formal_result.model_dump_json(indent=2).encode()
    result_path.write_bytes(result_payload)

    total_latency = _distribution(
        tuple(item.total_latency_ms for item in run_evaluations), "ms"
    )
    tool_latency = _distribution(
        tuple(item.tool_latency_ms for item in run_evaluations), "ms"
    )
    external_calls = _distribution(
        tuple(float(item.external_calls) for item in run_evaluations), "calls"
    )
    calls_by_kind = Counter(
        step.tool_kind or "UNCLASSIFIED"
        for trace in traces
        for step in trace.steps
        if step.kind == "tool"
    )
    pass_at_1 = sum(item.passed for item in run_evaluations) / len(run_evaluations)
    pass_power = sum(item.all_passed for item in case_evaluations) / len(case_evaluations)
    pass_at_attempts = sum(item.any_passed for item in case_evaluations) / len(
        case_evaluations
    )
    mixed = sum(item.mixed_pass_result for item in case_evaluations) / len(
        case_evaluations
    )
    output_consistency = sum(item.output_consistent for item in case_evaluations) / len(
        case_evaluations
    )
    trajectory_consistency = sum(
        item.trajectory_consistent for item in case_evaluations
    ) / len(case_evaluations)
    deterministic_mode = source_summary.evaluation_mode == "deterministic_mock"
    deterministic_gate: GateStatus = (
        "pass"
        if deterministic_mode
        and pass_power == 1
        and mixed == 0
        and output_consistency == 1
        and trajectory_consistency == 1
        else "fail" if deterministic_mode else "not_evaluated"
    )
    real_gate: GateStatus = (
        "pass"
        if not deterministic_mode
        and expected_attempts == 3
        and pass_at_1 >= 0.85
        and pass_power >= 0.8
        and mixed <= 0.1
        else "fail"
        if not deterministic_mode and expected_attempts == 3
        else "not_evaluated"
    )
    token_status = _token_status(run_evaluations)
    cost_status = _cost_status(run_evaluations, price_table)
    summary = PerformanceStabilityRunSummary(
        evaluation_id=evaluation_id,
        project_version=__version__,
        generated_at=datetime.now(UTC),
        evaluation_mode=source_summary.evaluation_mode,
        dataset_id=source_summary.dataset_id,
        dataset_version=source_summary.dataset_version,
        dataset_sha256=source_summary.dataset_sha256,
        source_run_summary_file=str(summary_path),
        source_run_summary_sha256=_sha256(source_summary_payload),
        source_traces_file=str(trace_path),
        source_traces_sha256=_sha256(trace_payload),
        selected_cases=source_summary.selected_cases,
        attempts_per_case=source_summary.attempts_per_case,
        completed_runs=len(run_evaluations),
        passed_runs=sum(item.passed for item in run_evaluations),
        total_external_calls=sum(item.external_calls for item in run_evaluations),
        calls_by_kind=dict(sorted(calls_by_kind.items())),
        total_latency=total_latency,
        tool_latency=tool_latency,
        external_calls_per_run=external_calls,
        input_tokens_status=token_status,
        output_tokens_status=token_status,
        cost_status=cost_status,
        price_table_version=(
            price_table.price_table_version if price_table is not None else None
        ),
        price_table_sha256=price_table_sha256,
        performance_run_results_file=run_results_path.name,
        performance_run_results_sha256=_sha256(run_results_payload),
        stability_case_results_file=case_results_path.name,
        stability_case_results_sha256=_sha256(case_results_payload),
        evaluation_result_file=result_path.name,
        evaluation_result_sha256=_sha256(result_payload),
        pass_at_1=pass_at_1,
        pass_power_3=pass_power if expected_attempts == 3 else None,
        pass_at_3=pass_at_attempts if expected_attempts == 3 else None,
        mixed_run_rate=mixed if expected_attempts == 3 else None,
        output_consistency_rate=output_consistency,
        trajectory_consistency_rate=trajectory_consistency,
        deterministic_stability_gate=deterministic_gate,
        real_model_stability_gate=real_gate,
        cost_regression_gate="not_evaluated",
        latency_regression_gate="not_evaluated",
        stage_6_pipeline_passed=(
            len(run_evaluations) == source_summary.expected_runs
            and all(item.total_latency_ms >= 0 for item in run_evaluations)
            and expected_attempts == 3
        ),
        real_model_calls=sum(item.llm_calls for item in run_evaluations),
        limitations=_performance_limitations(
            deterministic_mode=deterministic_mode,
            expected_attempts=expected_attempts,
        ),
    )
    summary_path_out = output_root / "performance-stability-run-summary.json"
    summary_path_out.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    report_path = output_root / "evaluation-report.md"
    report_path.write_text(_render_report(summary, formal_result), encoding="utf-8")
    return summary


def _performance_limitations(
    *,
    deterministic_mode: bool,
    expected_attempts: int,
) -> tuple[str, ...]:
    if deterministic_mode:
        return (
            "This official run uses deterministic Mock inventory and orchestrator-controlled "
            "tool selection; its stability does not represent a stochastic model.",
            "Local Mock timings are instrumentation checks, not production SLA measurements.",
            "No LLM call occurred, so token and monetary metrics are not applicable and remain "
            "null rather than zero.",
            "Cost and latency regression gates require a comparable frozen baseline.",
        )
    if expected_attempts == 1:
        return (
            "This two-case, one-attempt run is a real-model contract preflight, not a stability "
            "evaluation.",
            "Inventory and providers remain deterministic Mock; only intent extraction uses the "
            "requested model.",
            "Token, latency, and cost values cover only the two preflight calls.",
            "The real-model stability gate remains not evaluated until 24 cases run three times.",
        )
    return (
        "The real model extracts intent while workflow planning and tool selection remain "
        "orchestrator-controlled.",
        "Inventory and providers are deterministic Mock rather than live travel services.",
        "Measured latency and cost apply to this frozen evaluation workload, not production SLA.",
        "Cost and latency regression gates require a comparable frozen real-model baseline.",
    )


def _evaluate_resource_run(
    trace: EvaluationTrace,
    run: CaseRunSummary,
    price_table: ModelPriceTable | None,
) -> ResourceRunEvaluation:
    if trace.case_id != run.case_id or trace.attempt != run.attempt:
        raise PerformanceEvaluationError(f"trace/run metadata mismatch: {trace.run_id}")
    elapsed_ms = max(
        0.0, (trace.completed_at - trace.started_at).total_seconds() * 1000
    )
    tool_steps = tuple(step for step in trace.steps if step.kind == "tool")
    llm_steps = tuple(step for step in tool_steps if step.tool_kind == "LLM")
    provider_steps = tuple(step for step in tool_steps if step.tool_kind == "PROVIDER")
    complete = tuple(
        step
        for step in llm_steps
        if step.token_usage is not None
        and step.token_usage.input_tokens is not None
        and step.token_usage.output_tokens is not None
    )
    token_complete: bool | None = len(complete) == len(llm_steps) if llm_steps else None
    input_tokens = (
        sum((step.token_usage.input_tokens or 0) for step in complete if step.token_usage)
        if llm_steps and token_complete
        else None
    )
    output_tokens = (
        sum((step.token_usage.output_tokens or 0) for step in complete if step.token_usage)
        if llm_steps and token_complete
        else None
    )
    estimated_cost = _estimate_run_cost(trace, llm_steps, price_table)
    trajectory_payload = [
        {
            "kind": step.kind,
            "status": step.status,
            "state_before": step.state_before,
            "state_after": step.state_after,
            "name": step.name,
            "tool_kind": step.tool_kind,
            # Audit payloads may contain intentionally random correlation IDs. They
            # are not agent decisions, so only tool I/O participates in the semantic
            # trajectory signature.
            "input_hash": step.input_hash if step.kind == "tool" else None,
            "output_hash": step.output_hash if step.kind == "tool" else None,
            "evidence_refs": step.evidence_refs,
            "error_type": step.error_type,
            "reason_code": step.reason_code,
        }
        for step in trace.steps
    ]
    return ResourceRunEvaluation(
        run_id=trace.run_id,
        case_id=trace.case_id,
        attempt=trace.attempt,
        passed=run.passed,
        total_latency_ms=round(elapsed_ms, 6),
        recorded_step_latency_ms=round(sum(step.duration_ms for step in trace.steps), 6),
        tool_latency_ms=round(sum(step.duration_ms for step in tool_steps), 6),
        llm_latency_ms=round(sum(step.duration_ms for step in llm_steps), 6),
        external_calls=len(tool_steps),
        provider_calls=len(provider_steps),
        llm_calls=len(llm_steps),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_usage_complete=token_complete,
        estimated_cost=estimated_cost,
        outcome_signature=stable_hash(trace.final.model_dump(mode="json")),
        trajectory_signature=stable_hash(trajectory_payload),
    )


def _estimate_run_cost(
    trace: EvaluationTrace,
    llm_steps: tuple,
    table: ModelPriceTable | None,
) -> float | None:
    if not llm_steps:
        return None
    model = trace.fingerprint.actual_model
    if table is None or table.status != "configured" or model not in table.models:
        return None
    price = table.models[model]
    total = 0.0
    for step in llm_steps:
        usage = step.token_usage
        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            return None
        total += (
            usage.input_tokens * price.input + usage.output_tokens * price.output
        ) / 1_000_000
    return round(total, 12)


def _validate_attempt_matrix(
    traces: tuple[EvaluationTrace, ...],
    *,
    selected_cases: int,
    expected_attempts: int,
) -> None:
    attempts: dict[str, list[int]] = defaultdict(list)
    for trace in traces:
        attempts[trace.case_id].append(trace.attempt)
    if len(attempts) != selected_cases:
        raise PerformanceEvaluationError("case count does not match source summary")
    expected = list(range(1, expected_attempts + 1))
    bad = sorted(case_id for case_id, values in attempts.items() if sorted(values) != expected)
    if bad:
        raise PerformanceEvaluationError(
            "incomplete or duplicate attempt matrix for: " + ", ".join(bad)
        )


def _evaluate_stability_cases(
    runs: tuple[ResourceRunEvaluation, ...],
) -> tuple[StabilityCaseEvaluation, ...]:
    by_case: dict[str, list[ResourceRunEvaluation]] = defaultdict(list)
    for run in runs:
        by_case[run.case_id].append(run)
    results: list[StabilityCaseEvaluation] = []
    for case_id in sorted(by_case):
        items = sorted(by_case[case_id], key=lambda item: item.attempt)
        passed = sum(item.passed for item in items)
        results.append(
            StabilityCaseEvaluation(
                case_id=case_id,
                attempts=len(items),
                passed_attempts=passed,
                all_passed=passed == len(items),
                any_passed=passed > 0,
                mixed_pass_result=0 < passed < len(items),
                output_consistent=len({item.outcome_signature for item in items}) == 1,
                trajectory_consistent=(
                    len({item.trajectory_signature for item in items}) == 1
                ),
                run_ids=tuple(item.run_id for item in items),
            )
        )
    return tuple(results)


def _build_evaluation_result(
    *,
    evaluation_id: str,
    source_summary: EvaluationRunSummary,
    run_evaluations: tuple[ResourceRunEvaluation, ...],
    case_evaluations: tuple[StabilityCaseEvaluation, ...],
    price_table: ModelPriceTable | None,
) -> EvaluationResult:
    total_runs = len(run_evaluations)
    total_cases = len(case_evaluations)
    passed_runs = sum(item.passed for item in run_evaluations)
    all_passed = sum(item.all_passed for item in case_evaluations)
    any_passed = sum(item.any_passed for item in case_evaluations)
    mixed = sum(item.mixed_pass_result for item in case_evaluations)
    output_consistent = sum(item.output_consistent for item in case_evaluations)
    trajectory_consistent = sum(item.trajectory_consistent for item in case_evaluations)
    latency = _distribution(
        tuple(item.total_latency_ms for item in run_evaluations), "ms"
    )
    tools = _distribution(
        tuple(item.tool_latency_ms for item in run_evaluations), "ms"
    )
    calls = _distribution(
        tuple(float(item.external_calls) for item in run_evaluations), "calls"
    )
    token_status = _token_status(run_evaluations)
    metrics = {
        "pass_at_1": _rate_metric(passed_runs, total_runs),
        "pass_power_3": _rate_metric(all_passed, total_cases),
        "pass_at_3": _rate_metric(any_passed, total_cases),
        "mixed_run_rate": _rate_metric(mixed, total_cases),
        "output_consistency_rate": _rate_metric(output_consistent, total_cases),
        "trajectory_consistency_rate": _rate_metric(
            trajectory_consistent, total_cases
        ),
        "total_latency_ms_mean": _value_metric(latency.mean, total_runs, "ms"),
        "total_latency_ms_p50": _value_metric(latency.p50, total_runs, "ms"),
        "total_latency_ms_p95": _value_metric(latency.p95, total_runs, "ms"),
        "total_latency_ms_max": _value_metric(latency.maximum, total_runs, "ms"),
        "tool_latency_ms_mean": _value_metric(tools.mean, total_runs, "ms"),
        "tool_latency_ms_p95": _value_metric(tools.p95, total_runs, "ms"),
        "external_calls_total": _value_metric(
            calls.total, total_runs, "calls"
        ),
        "external_calls_per_run_mean": _value_metric(calls.mean, total_runs, "calls"),
        "external_calls_per_run_p95": _value_metric(calls.p95, total_runs, "calls"),
        "input_tokens_total": _token_metric(
            run_evaluations, "input_tokens", token_status
        ),
        "output_tokens_total": _token_metric(
            run_evaluations, "output_tokens", token_status
        ),
        "total_tokens_per_run_mean": _token_mean_metric(
            run_evaluations, token_status
        ),
        "estimated_cost_total": _cost_metric(run_evaluations, price_table, False),
        "estimated_cost_per_successful_task": _cost_metric(
            run_evaluations, price_table, True
        ),
        "cost_regression_rate": _unavailable_metric(
            "rate", "No frozen Stage 7 baseline exists."
        ),
        "latency_p95_regression_rate": _unavailable_metric(
            "rate", "No frozen Stage 7 baseline exists."
        ),
        "real_model_stability_gate": _real_model_gate_metric(
            source_summary=source_summary,
            case_evaluations=case_evaluations,
            pass_at_1=passed_runs / total_runs if total_runs else 0.0,
        ),
    }
    hard_failures: list[HardFailure] = []
    bad_cases: list[BadCaseCandidate] = []
    for case in case_evaluations:
        reasons: list[str] = []
        if not case.all_passed:
            reasons.append("not_all_attempts_passed")
        if case.mixed_pass_result:
            reasons.append("mixed_pass_result")
        if not case.output_consistent:
            reasons.append("output_inconsistent")
        if not case.trajectory_consistent:
            reasons.append("trajectory_inconsistent")
        if reasons:
            failure_type = ",".join(reasons)
            hard_failures.append(
                HardFailure(
                    case_id=case.case_id,
                    run_id=case.run_ids[0],
                    failure_type=failure_type,
                    evidence=case.run_ids,
                )
            )
            bad_cases.append(
                BadCaseCandidate(
                    case_id=case.case_id,
                    failure_signature=stable_hash(
                        {"case_id": case.case_id, "failure_type": failure_type}
                    ),
                    review_status="pending",
                )
            )
    measured_costs = tuple(
        item.estimated_cost
        for item in run_evaluations
        if item.estimated_cost is not None
    )
    successful_costs = tuple(
        item.estimated_cost
        for item in run_evaluations
        if item.passed and item.estimated_cost is not None
    )
    cost = CostSummary(
        currency=price_table.currency if price_table is not None else None,
        price_table_version=(
            price_table.price_table_version if price_table is not None else None
        ),
        total=(sum(measured_costs) if measured_costs else None),
        per_successful_task=(
            sum(successful_costs) / len(successful_costs) if successful_costs else None
        ),
    )
    return EvaluationResult(
        evaluation_id=evaluation_id,
        generated_at=datetime.now(UTC),
        run_ids=tuple(item.run_id for item in run_evaluations),
        coverage=EvaluationCoverage(
            dataset_id=source_summary.dataset_id,
            dataset_version=source_summary.dataset_version,
            selected_cases=source_summary.selected_cases,
            completed_runs=total_runs,
            expected_runs=source_summary.expected_runs,
            slices={
                "evaluation_mode": source_summary.evaluation_mode,
                "attempts_per_case": source_summary.attempts_per_case,
            },
        ),
        metrics=metrics,
        judge=None,
        cost=cost,
        gate_status="not_evaluated",
        hard_failures=tuple(hard_failures),
        bad_case_candidates=tuple(bad_cases),
    )


def _token_status(
    runs: tuple[ResourceRunEvaluation, ...],
) -> Literal["measured", "unavailable", "not_applicable"]:
    llm_calls = sum(item.llm_calls for item in runs)
    if llm_calls == 0:
        return "not_applicable"
    if all(item.token_usage_complete for item in runs if item.llm_calls):
        return "measured"
    return "unavailable"


def _cost_status(
    runs: tuple[ResourceRunEvaluation, ...],
    table: ModelPriceTable | None,
) -> Literal["measured", "unavailable", "not_applicable"]:
    if not any(item.llm_calls for item in runs):
        return "not_applicable"
    if table is None or table.status != "configured":
        return "unavailable"
    if all(item.estimated_cost is not None for item in runs if item.llm_calls):
        return "measured"
    return "unavailable"


def _token_metric(
    runs: tuple[ResourceRunEvaluation, ...],
    field: Literal["input_tokens", "output_tokens"],
    status: Literal["measured", "unavailable", "not_applicable"],
) -> MetricResult:
    if status != "measured":
        return MetricResult(
            status=status,
            value=None,
            numerator=None,
            denominator=None,
            unit="tokens",
            exposure_note=(
                "No LLM calls occurred in this evaluation mode."
                if status == "not_applicable"
                else "At least one LLM call lacks complete token metadata."
            ),
        )
    values = tuple(getattr(item, field) for item in runs if item.llm_calls)
    total = sum(value for value in values if value is not None)
    return MetricResult(
        status="measured",
        value=float(total),
        numerator=float(total),
        denominator=float(len(values)),
        unit="tokens",
    )


def _token_mean_metric(
    runs: tuple[ResourceRunEvaluation, ...],
    status: Literal["measured", "unavailable", "not_applicable"],
) -> MetricResult:
    if status != "measured":
        return _null_metric(status, "tokens_per_run")
    values = tuple(
        (item.input_tokens or 0) + (item.output_tokens or 0)
        for item in runs
        if item.llm_calls
    )
    return MetricResult(
        status="measured",
        value=sum(values) / len(values),
        numerator=float(sum(values)),
        denominator=float(len(values)),
        unit="tokens_per_run",
    )


def _cost_metric(
    runs: tuple[ResourceRunEvaluation, ...],
    table: ModelPriceTable | None,
    successful_only: bool,
) -> MetricResult:
    status = _cost_status(runs, table)
    if status != "measured":
        return MetricResult(
            status=status,
            value=None,
            numerator=None,
            denominator=None,
            unit=table.currency if table is not None else None,
            exposure_note=(
                "No LLM calls occurred in this evaluation mode."
                if status == "not_applicable"
                else "Token usage or a configured price entry is missing."
            ),
        )
    selected = tuple(
        item
        for item in runs
        if item.llm_calls and (item.passed or not successful_only)
    )
    if successful_only and not selected:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=None,
            denominator=0.0,
            unit=table.currency if table is not None else None,
            exposure_note="No successful model run exists for a per-success cost.",
        )
    total = sum(item.estimated_cost or 0.0 for item in selected)
    value = total / len(selected) if successful_only else total
    return MetricResult(
        status="measured",
        value=value,
        numerator=total,
        denominator=float(len(selected)) if successful_only else 1.0,
        unit=table.currency if table is not None else None,
    )


def _distribution(values: tuple[float, ...], unit: str) -> Distribution:
    if not values:
        raise PerformanceEvaluationError("cannot summarize an empty distribution")
    ordered = sorted(values)
    return Distribution(
        count=len(ordered),
        total=round(sum(ordered), 6),
        mean=round(sum(ordered) / len(ordered), 6),
        p50=round(_nearest_rank(ordered, 0.50), 6),
        p95=round(_nearest_rank(ordered, 0.95), 6),
        maximum=round(ordered[-1], 6),
        unit=unit,
    )


def _nearest_rank(ordered: list[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _rate_metric(numerator: int, denominator: int) -> MetricResult:
    if denominator == 0:
        return _unavailable_metric("rate", "Metric denominator is zero.")
    return MetricResult(
        status="measured",
        value=numerator / denominator,
        numerator=float(numerator),
        denominator=float(denominator),
        unit="rate",
        confidence_note="Exact aggregation over the fixed attempt matrix.",
    )


def _value_metric(value: float, denominator: int, unit: str) -> MetricResult:
    return MetricResult(
        status="measured",
        value=value,
        numerator=value,
        denominator=float(denominator),
        unit=unit,
    )


def _real_model_gate_metric(
    *,
    source_summary: EvaluationRunSummary,
    case_evaluations: tuple[StabilityCaseEvaluation, ...],
    pass_at_1: float,
) -> MetricResult:
    """Report the protocol 8 real-model stability gate as measured or unavailable.

    A real-model run that misses the thresholds must read as `fail`. Reporting it
    as unavailable would let a measured failure pass for an unmeasured one.
    """

    if source_summary.evaluation_mode == "deterministic_mock":
        return _unavailable_metric(
            "boolean",
            "This source run is deterministic Mock and cannot satisfy the real-model gate.",
        )
    if not case_evaluations or any(item.attempts != 3 for item in case_evaluations):
        return _unavailable_metric(
            "boolean",
            "The real-model stability gate requires three attempts for every case.",
        )
    total = len(case_evaluations)
    pass_power = sum(item.all_passed for item in case_evaluations) / total
    mixed = sum(item.mixed_pass_result for item in case_evaluations) / total
    passed = pass_at_1 >= 0.85 and pass_power >= 0.80 and mixed <= 0.10
    return MetricResult(
        status="measured",
        value=float(passed),
        numerator=float(passed),
        denominator=1.0,
        unit="boolean",
        confidence_note=(
            f"Protocol 8 thresholds on {source_summary.evaluation_mode}: "
            f"pass_at_1 {pass_at_1:.3f} >= 0.85, pass_power_3 {pass_power:.3f} >= 0.80, "
            f"mixed_run_rate {mixed:.3f} <= 0.10."
        ),
    )


def _unavailable_metric(unit: str, note: str) -> MetricResult:
    return MetricResult(
        status="unavailable",
        value=None,
        numerator=None,
        denominator=None,
        unit=unit,
        exposure_note=note,
    )


def _null_metric(
    status: Literal["unavailable", "not_applicable"], unit: str
) -> MetricResult:
    return MetricResult(
        status=status,
        value=None,
        numerator=None,
        denominator=None,
        unit=unit,
    )


def _render_report(
    summary: PerformanceStabilityRunSummary,
    result: EvaluationResult,
) -> str:
    metrics = result.metrics
    token_status = summary.input_tokens_status
    cost_status = summary.cost_status
    failures = len(result.hard_failures)
    coverage_line = (
        f"- 案例：{summary.selected_cases}；每例 {summary.attempts_per_case} 次；"
        f"完成 {summary.completed_runs} 次"
    )
    total_latency_row = _distribution_row("整次运行耗时（ms）", summary.total_latency)
    tool_latency_row = _distribution_row("工具步骤耗时（ms）", summary.tool_latency)
    external_calls_row = _distribution_row(
        "外部调用数/运行", summary.external_calls_per_run
    )
    output_tokens_row = (
        f"| 输出 Token 总量 | {summary.output_tokens_status} | "
        f"{_format_metric(metrics['output_tokens_total'])} |"
    )
    unit_cost_row = (
        f"| 成功任务单位费用 | {cost_status} | "
        f"{_format_metric(metrics['estimated_cost_per_successful_task'])} |"
    )
    return f"""# 阶段 6：Token、时延、费用与三次运行稳定性评测

## 结论

- 阶段 6 统计链路：**{'通过' if summary.stage_6_pipeline_passed else '失败'}**。
- 确定性 Mock 三次稳定性门禁：**{summary.deterministic_stability_gate}**。
- 真实模型稳定性门禁：**{summary.real_model_stability_gate}**；本轮不得用 Mock 结果替代。
- 完整协议门禁：**未评测**。

## 运行指纹与数据

- Evaluation ID：`{summary.evaluation_id}`
- 模式：`{summary.evaluation_mode}`
- 数据集：`{summary.dataset_id}` v`{summary.dataset_version}`
- 数据 SHA-256：`{summary.dataset_sha256}`
{coverage_line}
- 源轨迹 SHA-256：`{summary.source_traces_sha256}`
- 价格表：`{summary.price_table_version}`；状态 `{cost_status}`

## 多次运行稳定性

| 指标 | 结果 | 门槛/解释 |
|---|---:|---|
| pass@1 | {summary.pass_at_1:.2%} | 运行成功率 |
| pass^3 | {_format_rate(summary.pass_power_3)} | 三次全部成功；真实模型门槛 >= 80% |
| pass@3 | {_format_rate(summary.pass_at_3)} | 三次至少成功一次，不替代稳定率 |
| mixed run rate | {_format_rate(summary.mixed_run_rate)} | 真实模型门槛 <= 10% |
| 输出一致率 | {_format_rate(summary.output_consistency_rate)} | 排除仅成功状态一致、结果实际漂移 |
| 轨迹一致率 | {_format_rate(summary.trajectory_consistency_rate)} | 忽略时间戳与耗时后比较轨迹 |

## 时延与外部调用

| 指标 | 均值 | P50 | P95 | 最大值 |
|---|---:|---:|---:|---:|
{total_latency_row}
{tool_latency_row}
{external_calls_row}

- 外部调用总数：{summary.total_external_calls}
- 按类型：`{json.dumps(summary.calls_by_kind, ensure_ascii=False, sort_keys=True)}`
- 时延回归门禁：`{summary.latency_regression_gate}`（阶段 7 冻结基线后启用）

## Token 与费用

| 指标 | 状态 | 值 |
|---|---|---:|
| 输入 Token 总量 | {token_status} | {_format_metric(metrics['input_tokens_total'])} |
{output_tokens_row}
| 每运行总 Token 均值 | {token_status} | {_format_metric(metrics['total_tokens_per_run_mean'])} |
| 估算总费用 | {cost_status} | {_format_metric(metrics['estimated_cost_total'])} |
{unit_cost_row}

`null` 表示不可用或不适用，不表示 0。模型重试若存在，会作为独立 LLM 步骤全部计入。

## 失败与回流候选

- 三次运行失败或不一致案例：{failures}
- 当前回流候选：{len(result.bad_case_candidates)}（阶段 7 处理）

## 本轮没有评测的内容

- D2 固定 24 条真实模型冒烟集每条 3 次（预计 72 次模型调用）：环境无 API Key，且未取得付费运行确认。
- 真实 Token、真实模型费用和网络时延。
- 与冻结基线的成本/时延回归；基线将在阶段 7 建立。

## 局限

{chr(10).join(f'- {item}' for item in summary.limitations)}
"""


def _format_rate(value: float | None) -> str:
    return "null" if value is None else f"{value:.2%}"


def _format_metric(metric: MetricResult) -> str:
    return "null" if metric.value is None else f"{metric.value:.6f}"


def _distribution_row(label: str, distribution: Distribution) -> str:
    return (
        f"| {label} | {distribution.mean:.3f} | {distribution.p50:.3f} | "
        f"{distribution.p95:.3f} | {distribution.maximum:.3f} |"
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
