"""评测什么：真实模型上线前的意图预检（稳定性/性能门禁）。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent.agent.ports import LanguageModelPort
from corporate_travel_agent.services.evaluation_model_runner import (
    run_model_intent_preflight_evaluation,
)
from corporate_travel_agent.services.evaluation_performance import (
    evaluate_performance_and_stability,
    load_model_price_table,
)
from corporate_travel_agent.services.evaluation_quality import EvaluationResult
from corporate_travel_agent.services.evaluation_runner import (
    EvaluationRunnerError,
    EvaluationRunSummary,
)
from corporate_travel_agent.services.evaluation_trace import EvaluationTrace

PREFLIGHT_PROTOCOL_VERSION = "real-model-preflight-v1"
SHA256_PATTERN = r"^[a-f0-9]{64}$"


class PreflightModel(BaseModel):
    """预检结果 Pydantic 模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class RealModelPreflightResult(PreflightModel):
    """真实模型预检结构化结果。"""
    schema_version: Literal[1] = 1
    protocol_version: Literal["real-model-preflight-v1"] = PREFLIGHT_PROTOCOL_VERSION
    generated_at: datetime
    status: Literal["pass", "fail"]
    requested_model: str = Field(min_length=1)
    dataset_id: Literal["intent-model-preflight-v1"]
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    case_ids: tuple[str, ...]
    expected_model_calls_max: Literal[2] = 2
    attempted_model_calls: int = Field(ge=0, le=2)
    successful_structured_output_calls: int = Field(ge=0, le=2)
    provider_calls: int = Field(ge=0)
    completed_runs: int = Field(ge=0, le=2)
    rule_quality_passed_runs: int = Field(ge=0, le=2)
    rule_quality_smoke_rate: float = Field(ge=0, le=1)
    trace_validation_passed: bool
    input_tokens_status: Literal["measured", "unavailable", "not_applicable"]
    output_tokens_status: Literal["measured", "unavailable", "not_applicable"]
    input_tokens_total: int | None = Field(default=None, ge=0)
    output_tokens_total: int | None = Field(default=None, ge=0)
    latency_p95_ms: float = Field(ge=0)
    cost_status: Literal["measured", "unavailable", "not_applicable"]
    estimated_cost_total: float | None = Field(default=None, ge=0)
    currency: str | None
    price_table_version: str
    price_table_sha256: str = Field(pattern=SHA256_PATTERN)
    contract_failures: tuple[str, ...]
    source_run_summary_sha256: str = Field(pattern=SHA256_PATTERN)
    performance_summary_sha256: str = Field(pattern=SHA256_PATTERN)
    limitations: tuple[str, ...]


def run_real_model_preflight(
    *,
    dataset_directory: str | Path,
    subset_path: str | Path,
    parent_subset_path: str | Path,
    output_directory: str | Path,
    price_table_path: str | Path,
    language_model: LanguageModelPort,
    code_revision: str | None = None,
) -> RealModelPreflightResult:
    """运行真实模型意图预检并产出门禁结论。"""

    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise EvaluationRunnerError(f"Output directory already exists: {output_root}")
    price_table, price_table_sha256 = load_model_price_table(price_table_path)
    requested_model = str(getattr(language_model, "model", "")).strip()
    if not requested_model or requested_model == "unknown-model":
        raise EvaluationRunnerError("an explicit real model is required for preflight")
    if price_table.status != "configured" or requested_model not in price_table.models:
        raise EvaluationRunnerError(
            "the price table must be configured and contain the requested model"
        )

    source_root = output_root / "source"
    run_summary = run_model_intent_preflight_evaluation(
        dataset_directory=dataset_directory,
        subset_path=subset_path,
        parent_subset_path=parent_subset_path,
        output_directory=source_root,
        language_model=language_model,
        code_revision=code_revision,
        price_table_version=price_table.price_table_version,
    )
    if run_summary.real_model_calls > 2:
        raise EvaluationRunnerError("preflight exceeded the two-call safety limit")

    analysis_root = output_root / "analysis"
    performance = evaluate_performance_and_stability(
        source_run_directory=source_root,
        output_directory=analysis_root,
        price_table_path=price_table_path,
        expected_attempts=1,
    )
    formal_result = EvaluationResult.model_validate_json(
        (analysis_root / performance.evaluation_result_file).read_bytes()
    )
    traces = _load_traces(source_root, run_summary)
    successful_structured_outputs = sum(
        step.kind == "tool"
        and step.tool_kind == "LLM"
        and step.status == "success"
        for trace in traces
        for step in trace.steps
    )
    provider_calls = performance.calls_by_kind.get("PROVIDER", 0)
    failures: list[str] = []
    if run_summary.completed_runs != 2:
        failures.append("two_runs_not_completed")
    if run_summary.real_model_calls != 2:
        failures.append("model_call_count_not_two")
    if successful_structured_outputs != 2:
        failures.append("structured_output_contract_failed")
    if not run_summary.trace_validation_passed:
        failures.append("trace_validation_failed")
    if performance.input_tokens_status != "measured":
        failures.append("input_tokens_unavailable")
    if performance.output_tokens_status != "measured":
        failures.append("output_tokens_unavailable")
    if performance.cost_status != "measured":
        failures.append("cost_unavailable")
    if provider_calls:
        failures.append("unexpected_provider_call")

    input_tokens = formal_result.metrics["input_tokens_total"].value
    output_tokens = formal_result.metrics["output_tokens_total"].value
    result = RealModelPreflightResult(
        generated_at=datetime.now(UTC),
        status="fail" if failures else "pass",
        requested_model=requested_model,
        dataset_id="intent-model-preflight-v1",
        dataset_sha256=run_summary.dataset_sha256,
        case_ids=tuple(item.case_id for item in run_summary.runs),
        attempted_model_calls=run_summary.real_model_calls,
        successful_structured_output_calls=successful_structured_outputs,
        provider_calls=provider_calls,
        completed_runs=run_summary.completed_runs,
        rule_quality_passed_runs=run_summary.passed_runs,
        rule_quality_smoke_rate=run_summary.passed_runs / run_summary.completed_runs,
        trace_validation_passed=run_summary.trace_validation_passed,
        input_tokens_status=performance.input_tokens_status,
        output_tokens_status=performance.output_tokens_status,
        input_tokens_total=int(input_tokens) if input_tokens is not None else None,
        output_tokens_total=int(output_tokens) if output_tokens is not None else None,
        latency_p95_ms=performance.total_latency.p95,
        cost_status=performance.cost_status,
        estimated_cost_total=(
            formal_result.cost.total if formal_result.cost is not None else None
        ),
        currency=price_table.currency,
        price_table_version=price_table.price_table_version,
        price_table_sha256=price_table_sha256,
        contract_failures=tuple(failures),
        source_run_summary_sha256=_sha256(
            (source_root / "run-summary.json").read_bytes()
        ),
        performance_summary_sha256=_sha256(
            (analysis_root / "performance-stability-run-summary.json").read_bytes()
        ),
        limitations=(
            "Preflight contract pass does not establish production quality or stability.",
            "Rule-quality results are a smoke signal only and do not block this connectivity "
            "qualification.",
            "Air, rail, hotel, booking, payment, and policy providers remain deterministic Mock.",
            "The formal real-model comparison still requires 24 frozen cases times 3 runs.",
        ),
    )
    (output_root / "preflight-result.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_root / "evaluation-report.md").write_text(
        _render_report(result), encoding="utf-8"
    )
    return result


def _load_traces(
    source_root: Path,
    summary: EvaluationRunSummary,
) -> tuple[EvaluationTrace, ...]:
    payload = (source_root / summary.traces_file).read_bytes()
    if _sha256(payload) != summary.traces_sha256:
        raise EvaluationRunnerError("preflight trace hash does not match run summary")
    return tuple(
        EvaluationTrace.model_validate_json(line)
        for line in payload.splitlines()
        if line.strip()
    )


def _render_report(result: RealModelPreflightResult) -> str:
    failures = ", ".join(result.contract_failures) or "none"
    return f"""# Real Model Preflight

- Status: **{result.status}**
- Model: `{result.requested_model}`
- Dataset: `{result.dataset_id}` (`{result.dataset_sha256}`)
- Cases: {", ".join(result.case_ids)}
- Model calls: {result.attempted_model_calls}/2 maximum
- Structured outputs: {result.successful_structured_output_calls}/2
- Provider calls: {result.provider_calls} (expected 0)
- Rule-quality smoke: {result.rule_quality_passed_runs}/{result.completed_runs}
- Token status: {result.input_tokens_status}/{result.output_tokens_status}
- Input/output tokens: {result.input_tokens_total}/{result.output_tokens_total}
- Latency p95: {result.latency_p95_ms:.3f} ms
- Cost: {result.estimated_cost_total} {result.currency or "unknown"}
- Contract failures: {failures}

This is a qualification preflight only. It does not replace the frozen 24-case x 3-run
real-model stability evaluation. Inventory and providers are deterministic Mock.
"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
