"""评测什么：真实模型 + 真实/冻结供应商路径的端到端意图与工作流评测。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corporate_travel_agent import __version__
from corporate_travel_agent.agent.ports import LanguageModelPort
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.models import TripRequestVersion
from corporate_travel_agent.providers.base import TravelInventoryProvider
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_performance import (
    ModelPriceTable,
    load_model_price_table,
)
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationTrace,
    EvaluationTraceRecorder,
    TraceFinal,
    TraceFingerprint,
)
from corporate_travel_agent.services.object_storage import (
    LocalRawResponseObjectStore,
    RawResponseObjectStore,
)
from corporate_travel_agent.services.policy_config import (
    LoadedPolicyConfiguration,
    load_policy_configuration,
)

MODEL_LIVE_PROVIDER_RUNNER_VERSION = "model-live-provider-eval-runner-v1"
MODEL_LIVE_PROVIDER_GRADER_VERSION = "model-live-provider-grader-v3"
SHA256_PATTERN = r"^[a-f0-9]{64}$"


class ModelLiveProviderEvaluationError(RuntimeError):
    """模型+实时供应商评测失败。"""
    pass


class LiveEvaluationModel(BaseModel):
    """实时供应商评测模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class FrozenArtifactReference(LiveEvaluationModel):
    """冻结产物引用（哈希与路径）。"""
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


class ExpectedIntent(LiveEvaluationModel):
    """期望意图字段。"""
    classification: Literal["TRIP"]
    origin: str
    destination: str
    departure_after: datetime
    arrive_by: datetime
    return_after: datetime | None
    return_before: datetime | None
    hotel_check_in: date | None
    hotel_check_out: date | None
    hard_constraints: tuple[str, ...]
    soft_preferences: tuple[str, ...]


class ExpectedLiveWorkflow(LiveEvaluationModel):
    """期望实时工作流终态。"""
    tool_sequence: tuple[str, ...] = Field(min_length=1)
    llm_tool_calls: Literal[1]
    maximum_llm_tool_calls: Literal[1, 2] = 1
    allowed_retry_reason_codes: tuple[str, ...] = ()
    provider_tool_calls: Literal[1]
    maximum_provider_tool_calls: Literal[1, 2] = 1
    allowed_provider_retry_reason_codes: tuple[str, ...] = ()
    provider_source_type: Literal["AUTHORIZED_API"]
    final_state: Literal["WAITING_FOR_USER"]
    minimum_options: int = Field(ge=1)
    booking_intent_created: Literal[False]
    test_mode_disclosed: Literal[True]
    raw_response_archived: Literal[True]

    @model_validator(mode="after")
    def llm_call_bounds_are_consistent(self) -> ExpectedLiveWorkflow:
        if self.maximum_llm_tool_calls < self.llm_tool_calls:
            raise ValueError("maximum LLM calls cannot be below minimum LLM calls")
        if self.maximum_llm_tool_calls == 1 and self.allowed_retry_reason_codes:
            raise ValueError("retry reason codes require a second LLM attempt")
        if self.maximum_provider_tool_calls < self.provider_tool_calls:
            raise ValueError("maximum provider calls cannot be below minimum calls")
        if self.maximum_provider_tool_calls == 1 and self.allowed_provider_retry_reason_codes:
            raise ValueError("provider retry reasons require a second provider attempt")
        return self


class ModelLiveProviderCase(LiveEvaluationModel):
    """模型+供应商评测用例。"""
    case_id: str = Field(pattern=r"^[a-z0-9-]{1,128}$")
    traveler_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=20_000)
    policy_currency: str = Field(pattern=r"^[A-Z]{3}$")
    expected_intent: ExpectedIntent
    expected: ExpectedLiveWorkflow


class ModelLiveProviderDataset(LiveEvaluationModel):
    """模型+供应商评测数据集。"""
    schema_version: Literal[1]
    dataset_id: Literal[
        "model-duffel-workflow-smoke-v1",
        "model-duffel-workflow-recovery-v1",
        "model-duffel-workflow-full-recovery-v1",
        "model-duffel-workflow-deepseek-full-recovery-v1",
    ]
    dataset_version: Literal["1.0.0"]
    status: Literal["frozen"]
    evaluation_mode: Literal["model_live_provider"]
    provider: Literal["duffel"]
    provider_mode: Literal["test"]
    tool_choice_exposure: Literal["orchestrator_controlled"]
    records: Literal[1]
    attempts_per_case: Literal[3]
    maximum_model_calls: int = Field(ge=3, le=6)
    maximum_provider_calls: int = Field(ge=3, le=6)
    max_llm_attempts_per_run: Literal[1, 2] = 1
    max_provider_attempts_per_run: Literal[1, 2] = 1
    max_workflow_tool_calls_per_run: int = Field(default=2, ge=2, le=4)
    booking_enabled: Literal[False]
    requested_model: Literal["gpt-5.6", "deepseek-v4-pro"]
    reasoning_effort: Literal["medium"]
    max_output_tokens_per_call: Literal[1200]
    usage_contract: Literal["openai_detailed", "input_output_total"] = "openai_detailed"
    maximum_estimated_model_cost_usd: float = Field(gt=0, le=1)
    tool_registry: FrozenArtifactReference
    price_table: FrozenArtifactReference
    cases: tuple[ModelLiveProviderCase, ...]
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def frozen_counts_are_consistent(self) -> ModelLiveProviderDataset:
        if len(self.cases) != self.records:
            raise ValueError("dataset records do not match cases")
        expected_calls = self.records * self.attempts_per_case
        if self.maximum_model_calls != expected_calls * self.max_llm_attempts_per_run:
            raise ValueError("model-call cap must equal the frozen attempt matrix")
        if self.maximum_provider_calls != expected_calls * self.max_provider_attempts_per_run:
            raise ValueError("provider-call cap must equal the frozen attempt matrix")
        minimum_tool_budget = self.max_llm_attempts_per_run + self.max_provider_attempts_per_run
        if self.max_workflow_tool_calls_per_run < minimum_tool_budget:
            raise ValueError("workflow tool budget cannot cover LLM attempts and provider")
        if any(
            case.expected.maximum_llm_tool_calls != self.max_llm_attempts_per_run
            for case in self.cases
        ):
            raise ValueError("case LLM attempt bounds must match dataset retry policy")
        if any(
            case.expected.maximum_provider_tool_calls != self.max_provider_attempts_per_run
            for case in self.cases
        ):
            raise ValueError("case provider bounds must match dataset retry policy")
        return self


ProviderFactory = Callable[
    [RawResponseObjectStore, str],
    TravelInventoryProvider,
]


def load_model_live_provider_dataset(
    path: str | Path,
    *,
    project_root: str | Path,
) -> tuple[
    ModelLiveProviderDataset,
    str,
    frozenset[str],
    ModelPriceTable,
    str,
]:
    """加载模型+实时供应商评测数据集。"""
    root = Path(project_root).expanduser().resolve()
    dataset_path = Path(path).expanduser().resolve()
    payload = dataset_path.read_bytes()
    dataset = ModelLiveProviderDataset.model_validate_json(payload)
    tool_registry_path = _resolve_frozen_artifact(root, dataset.tool_registry)
    tool_registry = json.loads(tool_registry_path.read_text(encoding="utf-8"))
    tools = tool_registry.get("tools")
    if not isinstance(tools, list):
        raise ModelLiveProviderEvaluationError("tool registry has no tools list")
    tool_names = frozenset(
        item["name"]
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )
    if not tool_names:
        raise ModelLiveProviderEvaluationError("tool registry contains no tool names")
    price_path = _resolve_frozen_artifact(root, dataset.price_table)
    price_table, price_sha256 = load_model_price_table(price_path)
    if price_table.status != "configured":
        raise ModelLiveProviderEvaluationError("price table must be configured")
    return (
        dataset,
        hashlib.sha256(payload).hexdigest(),
        tool_names,
        price_table,
        price_sha256,
    )


def run_model_live_provider_evaluation(
    *,
    dataset_path: str | Path,
    output_directory: str | Path,
    project_root: str | Path,
    language_model: LanguageModelPort,
    provider_factory: ProviderFactory,
    code_revision: str | None = None,
    clock: Callable[[], datetime] | None = None,
    retry_sleep: Callable[[float], None] | None = None,
    retry_backoff_base_seconds: float = 0.5,
) -> dict[str, Any]:
    """执行模型+实时供应商评测运行。"""
    (
        dataset,
        dataset_sha256,
        allowed_tools,
        price_table,
        price_sha256,
    ) = load_model_live_provider_dataset(dataset_path, project_root=project_root)
    _validate_runtime_model(dataset, language_model, price_table)

    output = Path(output_directory).expanduser().resolve()
    if output.exists():
        raise ModelLiveProviderEvaluationError(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    raw_store = LocalRawResponseObjectStore(output / "raw-provider-responses")
    policy_configuration = _policy_configuration_for_currency(dataset.cases[0].policy_currency)

    traces: list[EvaluationTrace] = []
    run_results: list[dict[str, Any]] = []
    for case in dataset.cases:
        for attempt in range(1, dataset.attempts_per_case + 1):
            trace, result = _run_attempt(
                dataset=dataset,
                dataset_sha256=dataset_sha256,
                case=case,
                attempt=attempt,
                language_model=language_model,
                provider_factory=provider_factory,
                raw_store=raw_store,
                policy_configuration=policy_configuration,
                allowed_tools=allowed_tools,
                price_table=price_table,
                price_sha256=price_sha256,
                code_revision=code_revision,
                clock=clock,
                retry_sleep=retry_sleep,
                retry_backoff_base_seconds=retry_backoff_base_seconds,
            )
            traces.append(trace)
            run_results.append(result)

    summary = _build_summary(
        dataset=dataset,
        dataset_sha256=dataset_sha256,
        price_table=price_table,
        price_sha256=price_sha256,
        traces=traces,
        run_results=run_results,
    )
    trace_bytes = "".join(f"{trace.model_dump_json()}\n" for trace in traces).encode("utf-8")
    result_bytes = "".join(
        f"{json.dumps(item, ensure_ascii=False, sort_keys=True)}\n" for item in run_results
    ).encode("utf-8")
    (output / "traces.jsonl").write_bytes(trace_bytes)
    (output / "case-results.jsonl").write_bytes(result_bytes)
    summary["artifacts"] = {
        "traces_file": "traces.jsonl",
        "traces_sha256": hashlib.sha256(trace_bytes).hexdigest(),
        "case_results_file": "case-results.jsonl",
        "case_results_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "raw_response_root": "raw-provider-responses",
    }
    (output / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "evaluation-report.md").write_text(
        _render_report(summary),
        encoding="utf-8",
    )
    return summary


def regrade_model_live_provider_evaluation(
    *,
    dataset_path: str | Path,
    output_directory: str | Path,
    project_root: str | Path,
) -> dict[str, Any]:
    """对已有运行产物重新打分。"""
    (
        dataset,
        dataset_sha256,
        _,
        price_table,
        price_sha256,
    ) = load_model_live_provider_dataset(dataset_path, project_root=project_root)
    output = Path(output_directory).expanduser().resolve()
    trace_path = output / "traces.jsonl"
    result_path = output / "case-results.jsonl"
    if not trace_path.is_file() or not result_path.is_file():
        raise ModelLiveProviderEvaluationError(
            "existing traces.jsonl and case-results.jsonl are required"
        )
    traces = [
        EvaluationTrace.model_validate_json(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run_results = [
        json.loads(line)
        for line in result_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected = dataset.records * dataset.attempts_per_case
    if len(traces) != expected or len(run_results) != expected:
        raise ModelLiveProviderEvaluationError(
            f"existing run must contain exactly {expected} traces and results"
        )
    if any(trace.fingerprint.dataset_sha256 != dataset_sha256 for trace in traces):
        raise ModelLiveProviderEvaluationError(
            "existing trace dataset fingerprint does not match frozen dataset"
        )
    cases_by_id = {case.case_id: case for case in dataset.cases}
    regraded_results: list[dict[str, Any]] = []
    for trace, original in zip(traces, run_results, strict=True):
        case = cases_by_id.get(trace.case_id)
        if case is None or original.get("run_id") != trace.run_id:
            raise ModelLiveProviderEvaluationError(
                "existing trace and case-result identities do not match"
            )
        tool_steps = tuple(step for step in trace.steps if step.kind == "tool")
        llm_steps = tuple(step for step in tool_steps if step.tool_kind == "LLM")
        provider_steps = tuple(step for step in tool_steps if step.tool_kind == "PROVIDER")
        checks = dict(original["checks"])
        checks["retry_contract_valid"] = _llm_retry_contract_valid(case, llm_steps)
        checks["provider_retry_contract_valid"] = _provider_retry_contract_valid(
            case, provider_steps
        )
        regraded = {**original, "checks": checks, "passed": all(checks.values())}
        regraded_results.append(regraded)

    summary = _build_summary(
        dataset=dataset,
        dataset_sha256=dataset_sha256,
        price_table=price_table,
        price_sha256=price_sha256,
        traces=traces,
        run_results=regraded_results,
    )
    summary["artifacts"] = {
        "source_traces_file": "traces.jsonl",
        "source_traces_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
        "source_case_results_file": "case-results.jsonl",
        "source_case_results_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "raw_response_root": "raw-provider-responses",
        "regrade_only": True,
    }
    (output / "run-summary.regraded.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "evaluation-report.regraded.md").write_text(
        _render_report(summary),
        encoding="utf-8",
    )
    return summary


def _run_attempt(
    *,
    dataset: ModelLiveProviderDataset,
    dataset_sha256: str,
    case: ModelLiveProviderCase,
    attempt: int,
    language_model: LanguageModelPort,
    provider_factory: ProviderFactory,
    raw_store: RawResponseObjectStore,
    policy_configuration: LoadedPolicyConfiguration,
    allowed_tools: frozenset[str],
    price_table: ModelPriceTable,
    price_sha256: str,
    code_revision: str | None,
    clock: Callable[[], datetime] | None,
    retry_sleep: Callable[[float], None] | None,
    retry_backoff_base_seconds: float,
) -> tuple[EvaluationTrace, dict[str, Any]]:
    run_id = f"{case.case_id}-attempt-{attempt}"
    fingerprint = TraceFingerprint(
        project_version=__version__,
        code_revision=code_revision,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset_sha256,
        prompt_version=str(getattr(language_model, "prompt_version", "unknown")),
        requested_model=str(getattr(language_model, "model", "unknown")),
        actual_model=None,
        runner_version=MODEL_LIVE_PROVIDER_RUNNER_VERSION,
        price_table_version=price_table.price_table_version,
    )
    recorder = EvaluationTraceRecorder(
        run_id=run_id,
        case_id=case.case_id,
        attempt=attempt,
        evaluation_mode="model_live_provider",
        fingerprint=fingerprint,
        tool_choice_exposure=dataset.tool_choice_exposure,
    )
    provider = provider_factory(raw_store, case.policy_currency)
    workflow, _ = build_demo_system(
        language_model=language_model,
        provider=provider,
        clock=clock,
        policy_configuration=policy_configuration,
        trace_observer=recorder,
        max_tool_calls=dataset.max_workflow_tool_calls_per_run,
        max_provider_attempts=dataset.max_provider_attempts_per_run,
        max_llm_attempts=dataset.max_llm_attempts_per_run,
        retry_backoff_base_seconds=retry_backoff_base_seconds,
        retry_sleep=retry_sleep,
    )
    started_at = datetime.now(UTC)
    started_ns = perf_counter_ns()
    try:
        task = workflow.create_task_from_message(
            case.message,
            traveler_id=case.traveler_id,
            task_id=run_id,
        )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    completed_at = datetime.now(UTC)
    duration_ms = (perf_counter_ns() - started_ns) / 1_000_000
    snapshots = workflow.tasks.snapshots(task.task_id)
    result_refs = tuple(
        dict.fromkeys(ref for option in task.options for ref in option.inventory_refs)
    )
    selected = task.options[0] if task.options else None
    actual_model, llm_metadata = _llm_metadata(task.metadata)
    trace = recorder.finish(
        TraceFinal(
            state=task.state.value,
            policy_outcome=(
                selected.policy_decision.outcome.value if selected is not None else None
            ),
            booking_allowed=False,
            result_refs=result_refs,
            failure_reason=task.failure,
        )
    )
    trace = trace.model_copy(
        update={"fingerprint": trace.fingerprint.model_copy(update={"actual_model": actual_model})}
    )

    tool_steps = tuple(step for step in trace.steps if step.kind == "tool")
    tool_sequence = tuple(step.name or "" for step in tool_steps)
    llm_steps = tuple(step for step in tool_steps if step.tool_kind == "LLM")
    provider_steps = tuple(step for step in tool_steps if step.tool_kind == "PROVIDER")
    llm_failure_steps = tuple(step for step in llm_steps if step.status == "failure")
    provider_failure_steps = tuple(step for step in provider_steps if step.status == "failure")
    unknown_tools = tuple(name for name in tool_sequence if name not in allowed_tools)
    intent_mismatches = _intent_mismatches(case, task.request, task.metadata)
    provider_parameter_mismatches = _provider_parameter_mismatches(case, provider_steps)
    snapshot_refs = {item.ref_id for snapshot in snapshots for item in snapshot.items}
    evidence_refs = {ref for step in tool_steps for ref in step.evidence_refs}
    notices = tuple(task.metadata.get("provider_coverage_notices", ()))
    raw_archived = bool(snapshots) and all(
        snapshot.raw_response is not None for snapshot in snapshots
    )
    usage = _usage_from_metadata(llm_metadata)
    price_key = _price_key(
        actual_model=actual_model,
        requested_model=dataset.requested_model,
        table=price_table,
    )
    estimated_cost = _estimate_detailed_cost(usage, price_table, price_key)
    retry_contract_valid = _llm_retry_contract_valid(case, llm_steps)
    provider_retry_contract_valid = _provider_retry_contract_valid(case, provider_steps)
    checks = {
        "exact_tool_sequence": _tool_sequence_allowed(case, tool_sequence),
        "exact_llm_call_count": case.expected.llm_tool_calls
        <= len(llm_steps)
        <= case.expected.maximum_llm_tool_calls,
        "retry_contract_valid": retry_contract_valid,
        "provider_retry_contract_valid": provider_retry_contract_valid,
        "exact_provider_call_count": (
            case.expected.provider_tool_calls
            <= len(provider_steps)
            <= case.expected.maximum_provider_tool_calls
        ),
        "known_tool_registry_only": not unknown_tools,
        "exact_intent_parameters": not intent_mismatches,
        "exact_provider_parameters": not provider_parameter_mismatches,
        "authorized_api_snapshot": bool(snapshots)
        and all(
            snapshot.source_type.value == case.expected.provider_source_type
            for snapshot in snapshots
        ),
        "inventory_evidence_backed": bool(result_refs)
        and set(result_refs).issubset(snapshot_refs)
        and evidence_refs.issubset(snapshot_refs),
        "expected_final_state": task.state.value == case.expected.final_state,
        "minimum_options": len(task.options) >= case.expected.minimum_options,
        "booking_not_created": (task.booking_intent is not None)
        == case.expected.booking_intent_created,
        "test_mode_disclosed": any("Test Mode" in notice for notice in notices)
        == case.expected.test_mode_disclosed,
        "raw_response_archived": raw_archived == case.expected.raw_response_archived,
        "actual_model_recorded": bool(actual_model),
        "detailed_token_usage_recorded": _usage_contract_satisfied(
            dataset.usage_contract,
            usage,
        ),
        "price_mapping_available": price_key is not None and estimated_cost is not None,
    }
    recorded_failure = task.failure or task.metadata.get("recovered_extract_failure")
    return trace, {
        "run_id": run_id,
        "case_id": case.case_id,
        "attempt": attempt,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_ms": round(duration_ms, 3),
        "llm_latency_ms": round(sum(step.duration_ms for step in llm_steps), 3),
        "provider_latency_ms": round(sum(step.duration_ms for step in provider_steps), 3),
        "requested_model": dataset.requested_model,
        "actual_model": actual_model,
        "reasoning_effort": dataset.reasoning_effort,
        "service_tier": llm_metadata.get("service_tier"),
        "tool_sequence": tool_sequence,
        "unknown_tool_calls": unknown_tools,
        "intent_mismatches": intent_mismatches,
        "provider_parameter_mismatches": provider_parameter_mismatches,
        "llm_calls": len(llm_steps),
        "llm_retry_count": sum(step.retry_of is not None for step in llm_steps),
        "llm_error_diagnostics": tuple(
            {
                "error_type": step.error_type,
                "error_code": step.error_message_code,
                "error_layer": step.error_layer,
                "cause_type": step.error_cause_type,
                "cause_chain": step.error_cause_chain,
                "response_received": step.response_received,
                "http_status": step.http_status,
                "request_id": step.provider_request_id,
                "retryable": step.retryable,
            }
            for step in llm_failure_steps
        ),
        "provider_calls": len(provider_steps),
        "provider_retry_count": sum(step.retry_of is not None for step in provider_steps),
        "provider_error_diagnostics": tuple(
            {
                "error_type": step.error_type,
                "error_code": step.error_message_code,
                "error_layer": step.error_layer,
                "cause_type": step.error_cause_type,
                "cause_chain": step.error_cause_chain,
                "response_received": step.response_received,
                "http_status": step.http_status,
                "request_id": step.provider_request_id,
                "retryable": step.retryable,
            }
            for step in provider_failure_steps
        ),
        "task_state": task.state.value,
        "option_count": len(task.options),
        "snapshot_count": len(snapshots),
        "normalized_offer_count": sum(len(snapshot.items) for snapshot in snapshots),
        "raw_response_hashes": tuple(snapshot.raw_payload_hash for snapshot in snapshots),
        "booking_intent_created": task.booking_intent is not None,
        "provider_notices": notices,
        "usage": usage,
        "billing_price_key": price_key,
        "estimated_cost_usd": estimated_cost,
        "price_table_sha256": price_sha256,
        "intent_signature": stable_hash(_intent_projection(task.request, task.metadata)),
        "trajectory_signature": stable_hash(
            {
                "tools": tool_sequence,
                "statuses": tuple(step.status for step in tool_steps),
                "final_state": task.state.value,
            }
        ),
        "checks": checks,
        "passed": all(checks.values()),
        "failure": recorded_failure,
        "failure_category": _failure_category(recorded_failure),
        "parameter_hallucination_evaluable": actual_model is not None,
        "shadow_evidence_evaluable": bool(task.options),
    }


def _build_summary(
    *,
    dataset: ModelLiveProviderDataset,
    dataset_sha256: str,
    price_table: ModelPriceTable,
    price_sha256: str,
    traces: list[EvaluationTrace],
    run_results: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(bool(item["passed"]) for item in run_results)
    completed = len(run_results)
    all_passed = passed == completed
    any_passed = passed > 0
    intent_consistent = len({item["intent_signature"] for item in run_results}) == 1
    trajectory_consistent = len({item["trajectory_signature"] for item in run_results}) == 1
    token_names = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    token_totals: dict[str, int | None] = {}
    for name in token_names:
        values = [item["usage"][name] for item in run_results]
        token_totals[name] = (
            sum(int(value) for value in values if value is not None)
            if all(value is not None for value in values)
            else None
        )
    costs = [item["estimated_cost_usd"] for item in run_results]
    total_cost = sum(float(value) for value in costs if value is not None)
    unknown_calls = sum(len(item["unknown_tool_calls"]) for item in run_results)
    parameter_evaluable = [item for item in run_results if _parameter_hallucination_evaluable(item)]
    parameter_failures = sum(
        bool(item["intent_mismatches"] or item["provider_parameter_mismatches"])
        for item in parameter_evaluable
    )
    shadow_evaluable = [item for item in run_results if _shadow_evidence_evaluable(item)]
    shadow_proxy_failures = sum(
        not item["checks"]["inventory_evidence_backed"] for item in shadow_evaluable
    )
    failure_categories = [
        category
        for item in run_results
        if (category := _failure_category(item.get("failure"))) is not None
    ]
    failures_by_category = {
        category: failure_categories.count(category) for category in sorted(set(failure_categories))
    }
    infrastructure_failures = sum(
        category.startswith("infrastructure_") for category in failure_categories
    )
    accounted_attempts = sum(
        item["checks"]["detailed_token_usage_recorded"]
        and item["checks"]["price_mapping_available"]
        for item in run_results
    )
    completed_model_responses = sum(bool(item.get("actual_model")) for item in run_results)
    accounted_model_responses = sum(
        bool(item.get("actual_model"))
        and item["checks"]["detailed_token_usage_recorded"]
        and item["checks"]["price_mapping_available"]
        for item in run_results
    )
    llm_trace_steps = [step for trace in traces for step in trace.steps if step.tool_kind == "LLM"]
    failed_llm_requests = [step for step in llm_trace_steps if step.status == "failure"]
    retried_llm_requests = [step for step in llm_trace_steps if step.retry_of is not None]
    recovered_llm_retries = [step for step in retried_llm_requests if step.status == "success"]
    retry_error_layers = {
        layer: sum(step.error_layer == layer for step in failed_llm_requests)
        for layer in sorted({step.error_layer for step in failed_llm_requests if step.error_layer})
    }
    provider_trace_steps = [
        step for trace in traces for step in trace.steps if step.tool_kind == "PROVIDER"
    ]
    failed_provider_requests = [step for step in provider_trace_steps if step.status == "failure"]
    retried_provider_requests = [step for step in provider_trace_steps if step.retry_of is not None]
    recovered_provider_retries = [
        step for step in retried_provider_requests if step.status == "success"
    ]
    provider_retry_error_layers = {
        layer: sum(step.error_layer == layer for step in failed_provider_requests)
        for layer in sorted(
            {step.error_layer for step in failed_provider_requests if step.error_layer}
        )
    }
    llm_terminal_error_causes = _terminal_error_cause_counts(failed_llm_requests)
    provider_terminal_error_causes = _terminal_error_cause_counts(failed_provider_requests)
    common_terminal_error_causes = sorted(
        set(llm_terminal_error_causes) & set(provider_terminal_error_causes)
    )
    total_tool_calls = sum(len(item["tool_sequence"]) for item in run_results)
    raw_duplicate_calls = sum(
        len(item["tool_sequence"]) - len(set(item["tool_sequence"])) for item in run_results
    )
    retry_calls = sum(
        item["llm_retry_count"] + item["provider_retry_count"] for item in run_results
    )
    return {
        "schema_version": 1,
        "runner_version": MODEL_LIVE_PROVIDER_RUNNER_VERSION,
        "grader_version": MODEL_LIVE_PROVIDER_GRADER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "evaluation_mode": dataset.evaluation_mode,
        "provider": dataset.provider,
        "provider_mode": dataset.provider_mode,
        "tool_choice_exposure": dataset.tool_choice_exposure,
        "dataset": {
            "id": dataset.dataset_id,
            "version": dataset.dataset_version,
            "sha256": dataset_sha256,
            "records": dataset.records,
            "attempts_per_case": dataset.attempts_per_case,
        },
        "model": {
            "requested": dataset.requested_model,
            "actual_models": sorted(
                {item["actual_model"] for item in run_results if item["actual_model"]}
            ),
            "reasoning_effort": dataset.reasoning_effort,
            "usage_contract": dataset.usage_contract,
        },
        "price_table": {
            "version": price_table.price_table_version,
            "sha256": price_sha256,
            "currency": price_table.currency,
        },
        "runs": {
            "expected": dataset.records * dataset.attempts_per_case,
            "completed": completed,
            "passed": passed,
            "pass_at_1": passed / completed,
            "pass_power_3": 1.0 if all_passed else 0.0,
            "pass_at_3": 1.0 if any_passed else 0.0,
            "mixed_run_rate": 1.0 if any_passed and not all_passed else 0.0,
            "intent_consistency_rate": 1.0 if intent_consistent else 0.0,
            "trajectory_consistency_rate": 1.0 if trajectory_consistent else 0.0,
        },
        "calls": {
            "model": sum(item["llm_calls"] for item in run_results),
            "provider": sum(item["provider_calls"] for item in run_results),
            "model_cap": dataset.maximum_model_calls,
            "provider_cap": dataset.maximum_provider_calls,
        },
        "latency_ms": {
            "total": _distribution(item["duration_ms"] for item in run_results),
            "llm": _distribution(item["llm_latency_ms"] for item in run_results),
            "provider": _distribution(item["provider_latency_ms"] for item in run_results),
        },
        "tokens": token_totals,
        "estimated_cost_usd": round(total_cost, 9),
        "hallucination": {
            "tool_hallucination": {
                "model_exposure": "not_exposed",
                "system_unknown_tool_call_rate": unknown_calls
                / max(sum(len(item["tool_sequence"]) for item in run_results), 1),
                "count": unknown_calls,
            },
            "parameter_hallucination": {
                "model_exposure": "measured",
                "evaluable_attempts": len(parameter_evaluable),
                "not_evaluable_attempts": completed - len(parameter_evaluable),
                "failed_attempt_rate": parameter_failures / max(len(parameter_evaluable), 1),
                "failed_attempts": parameter_failures,
            },
            "shadow_hallucination": {
                "model_exposure": "not_exposed",
                "evaluable_attempts": len(shadow_evaluable),
                "not_evaluable_attempts": completed - len(shadow_evaluable),
                "system_evidence_proxy_failure_rate": shadow_proxy_failures
                / max(len(shadow_evaluable), 1),
                "failed_attempts": shadow_proxy_failures,
            },
        },
        "operational_reliability": {
            "infrastructure_failures": infrastructure_failures,
            "infrastructure_failure_rate": infrastructure_failures / completed,
            "completed_model_response_rate": completed_model_responses / completed,
            "failures_by_category": failures_by_category,
        },
        "resource_accounting": {
            "accounted_attempts": accounted_attempts,
            "unaccounted_attempts": completed - accounted_attempts,
            "all_attempt_accounting_coverage": accounted_attempts / completed,
            "completed_model_responses": completed_model_responses,
            "accounted_model_responses": accounted_model_responses,
            "completed_response_accounting_coverage": accounted_model_responses
            / max(completed_model_responses, 1),
            "estimated_cost_is_lower_bound": accounted_attempts < completed
            or bool(failed_llm_requests),
            "outbound_requests": len(llm_trace_steps) + len(provider_trace_steps),
            "traced_outbound_requests": len(llm_trace_steps) + len(provider_trace_steps),
            "outbound_request_trace_coverage": 1.0,
            "failed_before_response_requests": sum(
                step.response_received is False
                for step in (*failed_llm_requests, *failed_provider_requests)
            ),
        },
        "retries": {
            "maximum_llm_attempts_per_run": dataset.max_llm_attempts_per_run,
            "external_model_request_cap": dataset.maximum_model_calls,
            "failed_model_requests": len(failed_llm_requests),
            "retry_attempts": len(retried_llm_requests),
            "recovered_retries": len(recovered_llm_retries),
            "error_layers": retry_error_layers,
            "terminal_error_causes": llm_terminal_error_causes,
            "maximum_provider_attempts_per_run": (dataset.max_provider_attempts_per_run),
            "external_provider_request_cap": dataset.maximum_provider_calls,
            "failed_provider_requests": len(failed_provider_requests),
            "provider_retry_attempts": len(retried_provider_requests),
            "recovered_provider_retries": len(recovered_provider_retries),
            "provider_error_layers": provider_retry_error_layers,
            "provider_terminal_error_causes": provider_terminal_error_causes,
            "common_terminal_error_causes": common_terminal_error_causes,
        },
        "tool_efficiency": {
            "expected_calls_per_run": 2,
            "actual_calls_per_run": total_tool_calls / completed,
            "invalid_calls": unknown_calls,
            "duplicate_calls": raw_duplicate_calls,
            "authorized_retry_calls": retry_calls,
            "unjustified_duplicate_calls": max(raw_duplicate_calls - retry_calls, 0),
            "retry_overhead_rate": retry_calls / max(total_tool_calls, 1),
            "calls_per_successful_run": total_tool_calls / max(passed, 1),
            "order_pass_rate": sum(item["checks"]["exact_tool_sequence"] for item in run_results)
            / completed,
        },
        "gates": {
            "integration_gate": all_passed,
            "three_run_stability_gate": all_passed and intent_consistent and trajectory_consistent,
            "resource_accounting_gate": all(
                item["checks"]["detailed_token_usage_recorded"]
                and item["checks"]["price_mapping_available"]
                for item in run_results
            )
            and not failed_llm_requests,
            "booking_safety_gate": all(
                item["checks"]["booking_not_created"] for item in run_results
            ),
            "cost_ceiling_gate": total_cost <= dataset.maximum_estimated_model_cost_usd,
        },
        "maximum_estimated_model_cost_usd": (dataset.maximum_estimated_model_cost_usd),
        "attempt_results": run_results,
        "trace_count": len(traces),
        "limitations": dataset.limitations,
    }


def _render_report(summary: Mapping[str, Any]) -> str:
    gates = summary["gates"]
    status = "PASS" if all(gates.values()) else "FAIL"
    runs = summary["runs"]
    calls = summary["calls"]
    tokens = summary["tokens"]
    hallucination = summary["hallucination"]
    reliability = summary["operational_reliability"]
    accounting = summary["resource_accounting"]
    retries = summary["retries"]
    latency = summary["latency_ms"]
    gate_lines = "\n".join(
        f"- {'PASS' if value else 'FAIL'} `{name}`" for name, value in gates.items()
    )
    attempt_lines = "\n".join(
        f"- attempt {item['attempt']}: {'PASS' if item['passed'] else 'FAIL'}, "
        f"state `{item['task_state']}`, options {item['option_count']}, "
        f"cost `${item['estimated_cost_usd']}`, failure category "
        f"`{_failure_category(item.get('failure')) or 'none'}`"
        for item in summary["attempt_results"]
    )
    limitation_lines = "\n".join(f"- {item}" for item in summary["limitations"])
    token_line = f"{tokens['input_tokens']} / {tokens['output_tokens']} / {tokens['total_tokens']}"
    detailed_token_line = (
        f"{tokens['cached_input_tokens']} / "
        f"{tokens['cache_write_input_tokens']} / "
        f"{tokens['reasoning_output_tokens']}"
    )
    tool_hallucination_line = (
        f"model exposure `{hallucination['tool_hallucination']['model_exposure']}`; "
        f"system unknown calls {hallucination['tool_hallucination']['count']}"
    )
    parameter_hallucination_line = (
        "model exposure "
        f"`{hallucination['parameter_hallucination']['model_exposure']}`; "
        f"failed/evaluable attempts "
        f"{hallucination['parameter_hallucination']['failed_attempts']}/"
        f"{hallucination['parameter_hallucination']['evaluable_attempts']}; "
        f"not evaluable "
        f"{hallucination['parameter_hallucination']['not_evaluable_attempts']}"
    )
    shadow_hallucination_line = (
        f"model exposure `{hallucination['shadow_hallucination']['model_exposure']}`; "
        "evidence-proxy failed/evaluable attempts "
        f"{hallucination['shadow_hallucination']['failed_attempts']}/"
        f"{hallucination['shadow_hallucination']['evaluable_attempts']}; "
        f"not evaluable "
        f"{hallucination['shadow_hallucination']['not_evaluable_attempts']}"
    )
    infrastructure_line = (
        f"{reliability['infrastructure_failures']}/{runs['completed']} "
        f"({reliability['infrastructure_failure_rate']:.2%})"
    )
    resource_attempt_line = (
        f"{accounting['accounted_attempts']}/{runs['completed']} "
        f"({accounting['all_attempt_accounting_coverage']:.2%})"
    )
    response_accounting_line = (
        f"{accounting['accounted_model_responses']}/"
        f"{accounting['completed_model_responses']} "
        f"({accounting['completed_response_accounting_coverage']:.2%})"
    )
    retry_outcome_line = (
        f"{retries['failed_model_requests']} / {retries['retry_attempts']} / "
        f"{retries['recovered_retries']}"
    )
    retry_cap_line = (
        f"{retries['external_model_request_cap']}; maximum attempts per run: "
        f"{retries['maximum_llm_attempts_per_run']}"
    )
    provider_retry_outcome_line = (
        f"{retries['failed_provider_requests']} / "
        f"{retries['provider_retry_attempts']} / "
        f"{retries['recovered_provider_retries']}"
    )
    provider_retry_cap_line = (
        f"{retries['external_provider_request_cap']}; maximum attempts per run: "
        f"{retries['maximum_provider_attempts_per_run']}"
    )
    request_trace_line = (
        f"{accounting['traced_outbound_requests']}/"
        f"{accounting['outbound_requests']} "
        f"({accounting['outbound_request_trace_coverage']:.2%})"
    )
    efficiency = summary["tool_efficiency"]
    provider_terminal_causes_line = json.dumps(
        retries["provider_terminal_error_causes"], sort_keys=True
    )
    calls_per_run_line = (
        f"{efficiency['expected_calls_per_run']} / {efficiency['actual_calls_per_run']:.3f}"
    )
    return f"""# Real model + Duffel workflow evaluation

- Result: **{status}**
- Mode: `{summary["evaluation_mode"]}`
- Dataset: `{summary["dataset"]["id"]}` `{summary["dataset"]["version"]}`
- Dataset SHA-256: `{summary["dataset"]["sha256"]}`
- Requested model: `{summary["model"]["requested"]}`
- Actual models: `{", ".join(summary["model"]["actual_models"]) or "unavailable"}`
- Reasoning effort: `{summary["model"]["reasoning_effort"]}`
- Provider: `{summary["provider"]}` / `{summary["provider_mode"]}`

## Three-run stability

- Completed: {runs["completed"]}/{runs["expected"]}; passed: {runs["passed"]}
- pass@1: {runs["pass_at_1"]:.2%}
- pass^3: {runs["pass_power_3"]:.2%}
- pass@3: {runs["pass_at_3"]:.2%}
- mixed run rate: {runs["mixed_run_rate"]:.2%}
- intent consistency: {runs["intent_consistency_rate"]:.2%}
- trajectory consistency: {runs["trajectory_consistency_rate"]:.2%}

## Calls, latency, tokens, and cost

- Model calls: {calls["model"]}/{calls["model_cap"]}
- Duffel calls: {calls["provider"]}/{calls["provider_cap"]}
- Total latency mean/P95: {latency["total"]["mean"]:.3f} / {latency["total"]["p95"]:.3f} ms
- LLM latency mean/P95: {latency["llm"]["mean"]:.3f} / {latency["llm"]["p95"]:.3f} ms
- Provider latency mean/P95: {latency["provider"]["mean"]:.3f} / {latency["provider"]["p95"]:.3f} ms
- Input/output/total tokens: {token_line}
- Cached input/cache-write/reasoning tokens: {detailed_token_line}
- Estimated model cost: `${summary["estimated_cost_usd"]}` USD
- Frozen cost ceiling: `${summary["maximum_estimated_model_cost_usd"]}` USD
- Duffel Test Mode search cost: `$0` (no order or payment)

## Hallucination exposure

- Tool hallucination: {tool_hallucination_line}
- Parameter hallucination: {parameter_hallucination_line}
- Shadow hallucination: {shadow_hallucination_line}

## Operational reliability and accounting

- Infrastructure failures: {infrastructure_line}
- Completed model response rate: {reliability["completed_model_response_rate"]:.2%}
- Failure categories: `{json.dumps(reliability["failures_by_category"], sort_keys=True)}`
- Resource-accounted attempts: {resource_attempt_line}
- Completed-response accounting: {response_accounting_line}
- Outbound-request trace accounting: {request_trace_line}
- Failed before HTTP response: {accounting["failed_before_response_requests"]}
- Estimated cost is a lower bound: `{str(accounting["estimated_cost_is_lower_bound"]).lower()}`
- Failed model requests / retry attempts / recovered retries: {retry_outcome_line}
- Model request cap: {retry_cap_line}
- Retry error layers: `{json.dumps(retries["error_layers"], sort_keys=True)}`
- Model terminal error causes: `{json.dumps(retries["terminal_error_causes"], sort_keys=True)}`
- Failed provider requests / retry attempts / recovered retries: {provider_retry_outcome_line}
- Provider request cap: {provider_retry_cap_line}
- Provider retry error layers: `{json.dumps(retries["provider_error_layers"], sort_keys=True)}`
- Provider terminal error causes: `{provider_terminal_causes_line}`
- Common terminal error causes: `{json.dumps(retries["common_terminal_error_causes"])}`

## Tool efficiency

- Expected/actual calls per run: {calls_per_run_line}
- Raw repeated-name calls: {efficiency["duplicate_calls"]}
- Authorized retry calls: {efficiency["authorized_retry_calls"]}
- Unjustified duplicate calls: {efficiency["unjustified_duplicate_calls"]}
- Retry overhead rate: {efficiency["retry_overhead_rate"]:.2%}
- Calls per successful run: {efficiency["calls_per_successful_run"]:.3f}
- Allowed-order pass rate: {efficiency["order_pass_rate"]:.2%}

## Gates

{gate_lines}

## Attempts

{attempt_lines}

## Limitations

{limitation_lines}
"""


def _resolve_frozen_artifact(root: Path, reference: FrozenArtifactReference) -> Path:
    path = (root / reference.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ModelLiveProviderEvaluationError(
            f"frozen artifact is outside project or missing: {reference.path}"
        )
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != reference.sha256:
        raise ModelLiveProviderEvaluationError(
            f"frozen artifact fingerprint mismatch: {reference.path}"
        )
    return path


def _validate_runtime_model(
    dataset: ModelLiveProviderDataset,
    language_model: LanguageModelPort,
    price_table: ModelPriceTable,
) -> None:
    requested = str(getattr(language_model, "model", ""))
    effort = str(getattr(language_model, "reasoning_effort", ""))
    max_output_tokens = getattr(language_model, "max_output_tokens", None)
    if requested != dataset.requested_model:
        raise ModelLiveProviderEvaluationError(
            f"runtime model {requested!r} does not match frozen model {dataset.requested_model!r}"
        )
    if effort != dataset.reasoning_effort:
        raise ModelLiveProviderEvaluationError(
            f"runtime reasoning effort {effort!r} does not match frozen effort "
            f"{dataset.reasoning_effort!r}"
        )
    if max_output_tokens != dataset.max_output_tokens_per_call:
        raise ModelLiveProviderEvaluationError(
            f"runtime max output tokens {max_output_tokens!r} do not match frozen cap "
            f"{dataset.max_output_tokens_per_call!r}"
        )
    if dataset.requested_model not in price_table.models:
        raise ModelLiveProviderEvaluationError("requested model is missing from price table")


def _policy_configuration_for_currency(currency: str) -> LoadedPolicyConfiguration:
    loaded = load_policy_configuration()
    policies = tuple(
        replace(
            policy,
            currency=currency,
            content_hash=stable_hash(
                {
                    "source_policy_hash": policy.content_hash,
                    "evaluation_currency": currency,
                    "purpose": "model-duffel-workflow-smoke-v1",
                }
            ),
        )
        for policy in loaded.policy_snapshots
    )
    return replace(loaded, policy_snapshots=policies)


def _llm_metadata(metadata: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
    calls = metadata.get("llm_calls", ())
    if not isinstance(calls, (list, tuple)) or not calls:
        return None, {}
    call = calls[-1]
    if not isinstance(call, dict):
        return None, {}
    actual = call.get("model")
    return (str(actual) if actual else None), call


def _usage_from_metadata(metadata: Mapping[str, Any]) -> dict[str, int | None]:
    names = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {
        name: int(metadata[name]) if isinstance(metadata.get(name), int) else None for name in names
    }


def _usage_contract_satisfied(
    contract: str,
    usage: Mapping[str, int | None],
) -> bool:
    required = (
        (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )
        if contract == "openai_detailed"
        else ("input_tokens", "output_tokens", "total_tokens")
    )
    return all(usage[name] is not None for name in required)


def _intent_projection(
    request: TripRequestVersion | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    if request is None:
        return {"request": None, "classification": metadata.get("intent_classification")}
    return {
        "classification": metadata.get("intent_classification"),
        "origin": request.origin,
        "destination": request.destination,
        "departure_after": request.departure_after.isoformat(),
        "arrive_by": request.arrive_by.isoformat(),
        "return_after": request.return_after.isoformat() if request.return_after else None,
        "return_before": request.return_before.isoformat() if request.return_before else None,
        "hotel_check_in": request.hotel_check_in.isoformat() if request.hotel_check_in else None,
        "hotel_check_out": request.hotel_check_out.isoformat() if request.hotel_check_out else None,
        "hard_constraints": request.hard_constraints,
        "soft_preferences": request.soft_preferences,
    }


def _intent_mismatches(
    case: ModelLiveProviderCase,
    request: TripRequestVersion | None,
    metadata: Mapping[str, Any],
) -> tuple[str, ...]:
    actual = _intent_projection(request, metadata)
    expected = case.expected_intent.model_dump(mode="json")
    return tuple(
        name
        for name, value in expected.items()
        if _normalized(actual.get(name)) != _normalized(value)
    )


def _provider_parameter_mismatches(
    case: ModelLiveProviderCase,
    provider_steps: tuple[Any, ...],
) -> tuple[str, ...]:
    if not (
        case.expected.provider_tool_calls
        <= len(provider_steps)
        <= case.expected.maximum_provider_tool_calls
    ):
        return ("provider_call_count",)
    expected = case.expected_intent
    values = {
        "origin": expected.origin,
        "destination": expected.destination,
        "depart_after": expected.departure_after.isoformat(),
        "arrive_before": expected.arrive_by.isoformat(),
    }
    return tuple(
        f"attempt_{attempt}.{name}"
        for attempt, step in enumerate(provider_steps, start=1)
        for name, value in values.items()
        if _normalized((step.redacted_input or {}).get(name)) != _normalized(value)
    )


def _tool_sequence_allowed(
    case: ModelLiveProviderCase,
    actual: tuple[str, ...],
) -> bool:
    expected = case.expected.tool_sequence
    if not expected or len(expected) != 2:
        return False
    llm_count = actual.count(expected[0])
    provider_count = actual.count(expected[1])
    if not (case.expected.llm_tool_calls <= llm_count <= case.expected.maximum_llm_tool_calls):
        return False
    if not (
        case.expected.provider_tool_calls
        <= provider_count
        <= case.expected.maximum_provider_tool_calls
    ):
        return False
    allowed = (*([expected[0]] * llm_count), *([expected[1]] * provider_count))
    return actual == allowed


def _llm_retry_contract_valid(
    case: ModelLiveProviderCase,
    llm_steps: tuple[Any, ...],
) -> bool:
    return _retry_contract_valid(
        llm_steps,
        maximum_attempts=case.expected.maximum_llm_tool_calls,
        allowed_reason_codes=case.expected.allowed_retry_reason_codes,
    )


def _provider_retry_contract_valid(
    case: ModelLiveProviderCase,
    provider_steps: tuple[Any, ...],
) -> bool:
    return _retry_contract_valid(
        provider_steps,
        maximum_attempts=case.expected.maximum_provider_tool_calls,
        allowed_reason_codes=case.expected.allowed_provider_retry_reason_codes,
    )


def _retry_contract_valid(
    steps: tuple[Any, ...],
    *,
    maximum_attempts: int,
    allowed_reason_codes: tuple[str, ...],
) -> bool:
    """Judge retry-policy compliance separately from eventual task success."""

    if not steps:
        return True
    if len(steps) > maximum_attempts or steps[0].retry_of is not None:
        return False
    for previous, current in zip(steps, steps[1:], strict=False):
        if (
            previous.status != "failure"
            or previous.retryable is not True
            or current.retry_of != previous.tool_call_sequence
            or current.reason_code not in allowed_reason_codes
        ):
            return False
    final = steps[-1]
    if final.status == "success":
        return True
    if final.status != "failure":
        return False
    return final.retryable is not True or len(steps) == maximum_attempts


def _terminal_error_cause_counts(steps: list[Any]) -> dict[str, int]:
    causes = [
        (step.error_cause_chain[-1] if step.error_cause_chain else None)
        or step.error_cause_type
        or step.error_type
        or "unknown"
        for step in steps
    ]
    return {cause: causes.count(cause) for cause in sorted(set(causes))}


def _normalized(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, str) and ("T" in value or value.endswith("Z")):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat()
        except ValueError:
            return value
    return value


def _failure_category(failure: Any) -> str | None:
    if not isinstance(failure, str) or not failure.strip():
        return None
    normalized = failure.casefold()
    if "apiconnectionerror" in normalized or "connection error" in normalized:
        return "infrastructure_connection_error"
    if "apitimeouterror" in normalized or "timed out" in normalized:
        return "infrastructure_timeout"
    if "ratelimiterror" in normalized or "rate limit" in normalized:
        return "external_rate_limit"
    if "authenticationerror" in normalized or "authentication" in normalized:
        return "external_authentication_error"
    if "intent extraction failed" in normalized:
        return "model_or_parser_failure"
    if "provider" in normalized or "duffel" in normalized:
        return "provider_failure"
    return "workflow_failure"


def _parameter_hallucination_evaluable(item: Mapping[str, Any]) -> bool:
    explicit = item.get("parameter_hallucination_evaluable")
    if isinstance(explicit, bool):
        return explicit
    return bool(item.get("actual_model"))


def _shadow_evidence_evaluable(item: Mapping[str, Any]) -> bool:
    explicit = item.get("shadow_evidence_evaluable")
    if isinstance(explicit, bool):
        return explicit
    return bool(item.get("option_count"))


def _price_key(
    *,
    actual_model: str | None,
    requested_model: str,
    table: ModelPriceTable,
) -> str | None:
    if actual_model in table.models:
        return actual_model
    if actual_model and actual_model.startswith("gpt-5.6-sol"):
        return "gpt-5.6-sol" if "gpt-5.6-sol" in table.models else None
    if actual_model and actual_model.startswith("gpt-5.6"):
        return requested_model if requested_model in table.models else None
    return None


def _estimate_detailed_cost(
    usage: Mapping[str, int | None],
    table: ModelPriceTable,
    price_key: str | None,
) -> float | None:
    if price_key is None:
        return None
    if usage["input_tokens"] is None or usage["output_tokens"] is None:
        return None
    input_tokens = int(usage["input_tokens"] or 0)
    price = table.models[price_key]
    cached_value = usage["cached_input_tokens"]
    cache_write_value = usage["cache_write_input_tokens"]
    if (
        cached_value is not None
        and cache_write_value is not None
        and price.cached_input is not None
        and price.cache_write_input is not None
    ):
        cached = int(cached_value)
        cache_write = int(cache_write_value)
        uncached = input_tokens - cached - cache_write
        if uncached < 0:
            return None
        input_cost = (
            uncached * price.input
            + cached * price.cached_input
            + cache_write * price.cache_write_input
        )
    else:
        # Some OpenAI-compatible Chat APIs expose only aggregate prompt usage.
        # Charge every input token at the configured cache-miss rate so the
        # estimate remains a conservative upper bound.
        input_cost = input_tokens * price.input
    cost = (input_cost + int(usage["output_tokens"] or 0) * price.output) / 1_000_000
    return round(cost, 9)


def _distribution(values: Any) -> dict[str, float]:
    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    p50 = ordered[(count - 1) // 2]
    p95 = ordered[max(0, (95 * count + 99) // 100 - 1)]
    return {
        "count": float(count),
        "total": round(sum(ordered), 3),
        "mean": round(sum(ordered) / count, 3),
        "p50": round(p50, 3),
        "p95": round(p95, 3),
        "maximum": round(ordered[-1], 3),
    }
