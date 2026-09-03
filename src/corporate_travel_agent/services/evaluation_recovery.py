"""评测什么：故障恢复——供应商故障注入、超时/重试与进程重启后的安全恢复。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from corporate_travel_agent import __version__
from corporate_travel_agent.agent.orchestrator import TripWorkflowOrchestrator
from corporate_travel_agent.agent.ports import (
    WorkflowTraceEvent,
    WorkflowTraceObserverPort,
)
from corporate_travel_agent.domain.enums import (
    PolicyOutcome,
    TaskState,
    ToolCallStatus,
)
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    InventorySnapshot,
    LevelTravelRule,
    PolicySnapshot,
    ProviderHandoff,
    RevalidationResult,
    ToolCallRecord,
    TransportOffer,
    TravelOptionVersion,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
    inventory_query_hash,
)
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.audit import new_audit_event, stable_hash
from corporate_travel_agent.services.evaluation_dataset import (
    WorkflowEvaluationCase,
    WorkflowEvaluationObservation,
    load_evaluation_dataset,
)
from corporate_travel_agent.services.evaluation_quality import (
    BadCaseCandidate,
    EvaluationCoverage,
    EvaluationResult,
    HardFailure,
    MetricResult,
    project_user_output,
)
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationTrace,
    EvaluationTraceRecorder,
    TraceFinal,
    TraceFingerprint,
)
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    InMemoryTaskRepository,
    TaskRepository,
)

RECOVERY_EVALUATOR_VERSION: Final = "fault-recovery-evaluator-v1"
SHA256_PATTERN = r"^[a-f0-9]{64}$"

FaultType = Literal[
    "timeout",
    "retryable_error",
    "non_retryable_error",
    "permission_denied",
    "empty_result",
    "stale_snapshot",
    "partial_result",
    "process_restart",
]
InjectionPoint = Literal[
    "provider.search_transport.outbound",
    "provider.search_hotels",
    "provider.revalidate",
    "provider.create_deep_link",
]
FaultBehavior = Literal[
    "raise_once",
    "return_empty",
    "return_stale",
    "return_partial",
    "interrupt_started_call",
]
MutationDetection = Literal[
    "ungrounded_claim",
    "incorrect_booking_release",
    "lost_task_state",
    "missing_user_notice",
    "missing_restart_recovery",
]

SAFE_DEGRADATION_STATES = {
    TaskState.WAITING_FOR_PROVIDER,
    TaskState.PROVIDER_FAILED,
    TaskState.NO_FEASIBLE_OPTION,
    TaskState.RECONFIRMATION_REQUIRED,
}


class FaultEvaluationError(RuntimeError):
    """故障恢复评测失败。"""
    pass


class SimulatedTimeoutError(RetryableProviderError):
    """模拟超时的可重试供应商错误。"""
    pass


class SimulatedRetryableProviderError(RetryableProviderError):
    """模拟可重试供应商错误。"""
    pass


class SimulatedNonRetryableProviderError(ProviderError):
    """模拟不可重试供应商错误。"""
    pass


class SimulatedPermissionDeniedError(ProviderError):
    """模拟权限拒绝供应商错误。"""
    pass


class RecoveryModel(BaseModel):
    """恢复评测模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class FaultEvaluationCase(RecoveryModel):
    """故障恢复评测用例。"""
    case_id: str = Field(pattern=r"^fault-[a-z0-9-]+-[0-9]{2}$")
    base_case_id: str = Field(pattern=r"^prefer-[a-z0-9-]+$")
    fault_type: FaultType
    injection_point: InjectionPoint
    behavior: FaultBehavior
    autonomous_recovery_expected: bool
    safe_degradation_allowed: bool
    description: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def behavior_matches_fault(self) -> FaultEvaluationCase:
        expected_behavior = {
            "timeout": "raise_once",
            "retryable_error": "raise_once",
            "non_retryable_error": "raise_once",
            "permission_denied": "raise_once",
            "empty_result": "return_empty",
            "stale_snapshot": "return_stale",
            "partial_result": "return_partial",
            "process_restart": "interrupt_started_call",
        }[self.fault_type]
        if self.behavior != expected_behavior:
            raise ValueError("fault type and behavior do not match")
        if self.fault_type == "process_restart" and self.injection_point != (
            "provider.revalidate"
        ):
            raise ValueError("process restart fixtures must interrupt revalidation")
        return self


class FaultCaseFile(RecoveryModel):
    """故障用例文件包装。"""
    path: Literal["cases.jsonl"]
    records: Literal[16]
    sha256: str = Field(pattern=SHA256_PATTERN)


class FaultBaseDataset(RecoveryModel):
    """故障评测基线数据集。"""
    dataset_id: Literal["corporate-travel-derived-v2-workflow"]
    dataset_version: Literal["2.0.0"]
    path: str
    sha256: str = Field(pattern=SHA256_PATTERN)


class FaultDatasetManifest(RecoveryModel):
    """故障评测数据集清单。"""
    schema_version: Literal[1]
    dataset_id: Literal["fault-eval-v1"]
    dataset_version: Literal["1.0.0"]
    status: Literal["frozen"]
    cases: FaultCaseFile
    base_dataset: FaultBaseDataset
    fault_counts: dict[str, int]
    autonomous_recovery_candidate_records: int = Field(ge=1)
    safe_degradation_allowed_records: int = Field(ge=1)
    evaluation_mode: Literal["deterministic_mock"]
    real_model_calls: Literal[0]


@dataclass(frozen=True, slots=True)
class LoadedFaultDataset:
    """已加载的故障评测数据集。"""
    root: Path
    manifest: FaultDatasetManifest
    cases: tuple[FaultEvaluationCase, ...]
    manifest_sha256: str


class RecoveryCaseEvaluation(RecoveryModel):
    """单用例恢复评测结果。"""
    run_id: str
    case_id: str
    base_case_id: str
    fault_type: FaultType
    injection_point: InjectionPoint
    injection_triggered: bool
    final_state: str
    tool_calls: int = Field(ge=0)
    booking_intent_created: bool
    autonomous_recovery_expected: bool
    autonomous_recovery_success: bool
    safe_degradation_allowed: bool
    safe_degradation_success: bool
    unsafe_recovery: bool
    unsafe_reasons: tuple[str, ...]
    user_notified: bool
    recoverable_state_preserved: bool
    unsupported_claims: tuple[str, ...]
    recovery_attempts: int = Field(ge=0)
    recovery_latency_ms: float | None = Field(default=None, ge=0)
    total_latency_ms: float = Field(ge=0)
    restart_state_recovered: bool | None
    partial_result_disclosed: bool | None
    failure_reason: str | None
    trace_steps: int = Field(ge=1)


class RecoveryMutationCheck(RecoveryModel):
    """恢复评测器突变检查。"""
    mutation_id: str
    expected_detection: MutationDetection
    detected: bool


class RecoveryRunSummary(RecoveryModel):
    """恢复评测运行汇总。"""
    schema_version: Literal[1] = 1
    evaluation_id: str
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    evaluator_version: Literal["fault-recovery-evaluator-v1"] = (
        RECOVERY_EVALUATOR_VERSION
    )
    project_version: str
    generated_at: datetime
    evaluation_mode: Literal["deterministic_mock_fault_injection"]
    dataset_id: Literal["fault-eval-v1"]
    dataset_version: Literal["1.0.0"]
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    dataset_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    base_dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    selected_cases: Literal[16]
    completed_runs: int = Field(ge=0)
    traces_file: str
    traces_sha256: str = Field(pattern=SHA256_PATTERN)
    case_results_file: str
    case_results_sha256: str = Field(pattern=SHA256_PATTERN)
    evaluation_result_file: str
    evaluation_result_sha256: str = Field(pattern=SHA256_PATTERN)
    total_tool_calls: int = Field(ge=0)
    autonomous_recovery_candidates: int = Field(ge=0)
    autonomous_recoveries: int = Field(ge=0)
    safe_degradation_cases: int = Field(ge=0)
    safe_degradations: int = Field(ge=0)
    unsafe_recoveries: int = Field(ge=0)
    restart_cases: int = Field(ge=0)
    restart_state_recoveries: int = Field(ge=0)
    stage_5_gate_passed: bool
    full_protocol_gate: Literal["not_evaluated"] = "not_evaluated"
    real_model_calls: Literal[0] = 0
    estimated_cost: float | None = None
    mutation_checks: tuple[RecoveryMutationCheck, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Fixture:
    clock: Callable[[], datetime]
    employee: EmployeeProfileSnapshot
    policy: PolicySnapshot
    request: TripRequestVersion
    transports: tuple[TransportOffer, ...]
    hotels: tuple[HotelOffer, ...]


class _ScriptedFaultProvider:
    name = "derived-mock-fault-injection"

    def __init__(
        self,
        base: MockProvider,
        case: FaultEvaluationCase,
        observer: WorkflowTraceObserverPort,
    ) -> None:
        self.base = base
        self.case = case
        self.observer = observer
        self.target_invocations = 0
        self.transport_invocations = 0
        self._outbound_query_hash: str | None = None

    @property
    def injection_triggered(self) -> bool:
        return self.target_invocations > 0

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        self.transport_invocations += 1
        # Identify the leg by query identity, not call count: a bounded retry
        # repeats the same query and must stay labelled as the same leg.
        query_hash = inventory_query_hash(query)
        if self._outbound_query_hash is None:
            self._outbound_query_hash = query_hash
        point = (
            "provider.search_transport.outbound"
            if query_hash == self._outbound_query_hash
            else "provider.search_transport.inbound"
        )
        if point == self.case.injection_point:
            self._raise_if_needed(point)
        result = self.base.search_transport(query)
        if point == self.case.injection_point:
            return self._modify_snapshot(point, result)
        return result

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        point = "provider.search_hotels"
        if point == self.case.injection_point:
            self._raise_if_needed(point)
        result = self.base.search_hotels(query)
        if point == self.case.injection_point:
            return self._modify_snapshot(point, result)
        return result

    def revalidate(self, refs: tuple[str, ...]) -> RevalidationResult:
        point = "provider.revalidate"
        if point == self.case.injection_point:
            self._raise_if_needed(point)
        return self.base.revalidate(refs)

    def create_deep_link(self, option: TravelOptionVersion) -> ProviderHandoff:
        point = "provider.create_deep_link"
        if point == self.case.injection_point:
            self._raise_if_needed(point)
        return self.base.create_deep_link(option)

    def _raise_if_needed(self, point: str) -> None:
        if self.case.behavior != "raise_once" or self.target_invocations:
            return
        self.target_invocations += 1
        exception_type: type[ProviderError] = {
            "timeout": SimulatedTimeoutError,
            "retryable_error": SimulatedRetryableProviderError,
            "non_retryable_error": SimulatedNonRetryableProviderError,
            "permission_denied": SimulatedPermissionDeniedError,
        }[self.case.fault_type]
        self._record_fault(point, "failure", exception_type.__name__)
        raise exception_type(f"injected {self.case.fault_type} at {point}")

    def _modify_snapshot(
        self, point: str, snapshot: InventorySnapshot
    ) -> InventorySnapshot:
        if self.target_invocations:
            return snapshot
        self.target_invocations += 1
        if self.case.behavior == "return_empty":
            self._record_fault(point, "partial", None)
            return replace(
                snapshot,
                items=(),
                provider_warnings=(*snapshot.provider_warnings, "injected empty result"),
            )
        if self.case.behavior == "return_stale":
            self._record_fault(point, "failure", None)
            return replace(
                snapshot,
                valid_until=snapshot.captured_at,
                provider_warnings=(*snapshot.provider_warnings, "injected stale snapshot"),
            )
        if self.case.behavior == "return_partial":
            if len(snapshot.items) < 2:
                raise FaultEvaluationError(
                    f"{self.case.case_id} cannot inject a material partial result"
                )
            self._record_fault(point, "partial", None)
            return replace(
                snapshot,
                items=snapshot.items[:1],
                provider_warnings=(*snapshot.provider_warnings, "injected partial result"),
            )
        return snapshot

    def _record_fault(
        self, point: str, status: Literal["failure", "partial"], error_type: str | None
    ) -> None:
        self.observer.record(
            WorkflowTraceEvent(
                kind="recovery",
                name="FAULT_INJECTED",
                status=status,
                started_at=datetime.now(UTC),
                duration_ms=0.0,
                state_before=None,
                state_after=None,
                input_value={
                    "fault_type": self.case.fault_type,
                    "injection_point": point,
                },
                output_value={"behavior": self.case.behavior},
                error_type=error_type,
                reason_code=f"FAULT_{self.case.fault_type.upper()}",
            )
        )


def load_fault_evaluation_dataset(directory: str | Path) -> LoadedFaultDataset:
    """加载故障恢复评测数据集。"""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise FaultEvaluationError(f"fault dataset directory does not exist: {root}")
    manifest_path = root / "manifest.json"
    manifest_payload = manifest_path.read_bytes()
    manifest = FaultDatasetManifest.model_validate_json(manifest_payload)
    cases_path = (root / manifest.cases.path).resolve()
    if root not in cases_path.parents:
        raise FaultEvaluationError("fault case path escapes the dataset directory")
    cases_payload = cases_path.read_bytes()
    if _sha256(cases_payload) != manifest.cases.sha256:
        raise FaultEvaluationError("fault case file hash does not match manifest")
    adapter = TypeAdapter(FaultEvaluationCase)
    cases = tuple(
        adapter.validate_python(json.loads(line))
        for line in cases_payload.splitlines()
        if line.strip()
    )
    if len(cases) != manifest.cases.records:
        raise FaultEvaluationError("fault record count does not match manifest")
    if len({case.case_id for case in cases}) != len(cases):
        raise FaultEvaluationError("fault case IDs must be unique")
    counts = dict(Counter(case.fault_type for case in cases))
    if counts != manifest.fault_counts:
        raise FaultEvaluationError("fault category counts do not match manifest")
    if sum(case.autonomous_recovery_expected for case in cases) != (
        manifest.autonomous_recovery_candidate_records
    ):
        raise FaultEvaluationError("autonomous recovery count does not match manifest")
    if sum(case.safe_degradation_allowed for case in cases) != (
        manifest.safe_degradation_allowed_records
    ):
        raise FaultEvaluationError("safe degradation count does not match manifest")
    return LoadedFaultDataset(
        root=root,
        manifest=manifest,
        cases=cases,
        manifest_sha256=_sha256(manifest_payload),
    )


def evaluate_fault_recovery_run(
    *,
    fault_dataset_directory: str | Path,
    base_dataset_directory: str | Path,
    output_directory: str | Path,
    code_revision: str | None = None,
) -> RecoveryRunSummary:
    """执行故障恢复评测运行。"""
    fault_dataset = load_fault_evaluation_dataset(fault_dataset_directory)
    base_dataset = load_evaluation_dataset(base_dataset_directory)
    if base_dataset.manifest.workflow_cases.sha256 != (
        fault_dataset.manifest.base_dataset.sha256
    ):
        raise FaultEvaluationError("D5 base dataset fingerprint does not match D1")
    base_cases = {case.case_id: case for case in base_dataset.workflow_cases}
    missing = sorted(
        case.base_case_id
        for case in fault_dataset.cases
        if case.base_case_id not in base_cases
    )
    if missing:
        raise FaultEvaluationError(f"D5 references missing D1 cases: {missing}")
    if any(base_cases[case.base_case_id].fault.action != "NONE" for case in fault_dataset.cases):
        raise FaultEvaluationError("D5 base cases must not contain another injected fault")

    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise FaultEvaluationError(f"output directory already exists: {output_root}")
    output_root.mkdir(parents=True)

    fingerprint = TraceFingerprint(
        project_version=__version__,
        code_revision=code_revision,
        dataset_id=fault_dataset.manifest.dataset_id,
        dataset_version=fault_dataset.manifest.dataset_version,
        dataset_sha256=fault_dataset.manifest.cases.sha256,
        prompt_version=None,
        requested_model=None,
        actual_model=None,
        runner_version=RECOVERY_EVALUATOR_VERSION,
        price_table_version=None,
    )
    traces: list[EvaluationTrace] = []
    evaluations: list[RecoveryCaseEvaluation] = []
    for fault_case in fault_dataset.cases:
        run_id = f"run-{uuid4()}"
        recorder = EvaluationTraceRecorder(
            run_id=run_id,
            case_id=fault_case.case_id,
            attempt=1,
            evaluation_mode="deterministic_mock",
            fingerprint=fingerprint,
            tool_choice_exposure="orchestrator_controlled",
        )
        base_case = base_cases[fault_case.base_case_id]
        if fault_case.fault_type == "process_restart":
            observation, trace, injected, restart_recovered = _run_restart_case(
                fault_case=fault_case,
                base_case=base_case,
                run_id=run_id,
                recorder=recorder,
            )
        else:
            observation, trace, injected = _run_provider_fault_case(
                fault_case=fault_case,
                base_case=base_case,
                run_id=run_id,
                recorder=recorder,
            )
            restart_recovered = None
        evaluation = _evaluate_recovery_case(
            fault_case=fault_case,
            observation=observation,
            trace=trace,
            injection_triggered=injected,
            restart_state_recovered=restart_recovered,
        )
        traces.append(trace)
        evaluations.append(evaluation)

    evaluation_id = f"eval-{uuid4()}"
    formal_result = build_recovery_evaluation_result(
        evaluation_id=evaluation_id,
        evaluations=tuple(evaluations),
    )
    traces_path = output_root / "fault-traces.jsonl"
    traces_payload = "".join(f"{item.model_dump_json()}\n" for item in traces).encode()
    traces_path.write_bytes(traces_payload)
    cases_path = output_root / "fault-recovery-case-results.jsonl"
    cases_payload = "".join(
        f"{item.model_dump_json()}\n" for item in evaluations
    ).encode()
    cases_path.write_bytes(cases_payload)
    result_path = output_root / "fault-recovery-evaluation-result.json"
    result_payload = formal_result.model_dump_json(indent=2).encode()
    result_path.write_bytes(result_payload)

    autonomous = tuple(item for item in evaluations if item.autonomous_recovery_expected)
    # Safe degradation is only assessable where the task did not complete on its
    # own. A case that recovered autonomously never had to degrade, so scoring it
    # as a degradation failure would penalise the better outcome.
    degradable = tuple(
        item
        for item in evaluations
        if item.safe_degradation_allowed and not item.autonomous_recovery_success
    )
    restart = tuple(item for item in evaluations if item.restart_state_recovered is not None)
    summary = RecoveryRunSummary(
        evaluation_id=evaluation_id,
        project_version=__version__,
        generated_at=datetime.now(UTC),
        evaluation_mode="deterministic_mock_fault_injection",
        dataset_id="fault-eval-v1",
        dataset_version="1.0.0",
        dataset_sha256=fault_dataset.manifest.cases.sha256,
        dataset_manifest_sha256=fault_dataset.manifest_sha256,
        base_dataset_sha256=base_dataset.manifest.workflow_cases.sha256,
        selected_cases=16,
        completed_runs=len(evaluations),
        traces_file=traces_path.name,
        traces_sha256=_sha256(traces_payload),
        case_results_file=cases_path.name,
        case_results_sha256=_sha256(cases_payload),
        evaluation_result_file=result_path.name,
        evaluation_result_sha256=_sha256(result_payload),
        total_tool_calls=sum(item.tool_calls for item in evaluations),
        autonomous_recovery_candidates=len(autonomous),
        autonomous_recoveries=sum(item.autonomous_recovery_success for item in autonomous),
        safe_degradation_cases=len(degradable),
        safe_degradations=sum(item.safe_degradation_success for item in degradable),
        unsafe_recoveries=sum(item.unsafe_recovery for item in evaluations),
        restart_cases=len(restart),
        restart_state_recoveries=sum(bool(item.restart_state_recovered) for item in restart),
        stage_5_gate_passed=bool(formal_result.metrics["stage_5_rule_gate"].value),
        mutation_checks=_run_mutation_checks(),
        limitations=(
            "Faults are deterministic scripted Mock behaviors, not measured network "
            "or provider incidents.",
            "Timeout injection raises immediately; it does not sleep for a real timeout interval.",
            "Autonomous recovery counts only completion without an evaluator-triggered "
            "retry or replan.",
            "Process restart uses SQLite persistence and measures fail-closed state "
            "recovery, not automatic task completion.",
            "Partial responses expose provider warnings internally, but the deterministic "
            "user projection is evaluated separately for disclosure.",
            "No real model, external provider, token usage, or monetary cost is measured "
            "in this stage.",
        ),
    )
    summary_path = output_root / "fault-recovery-run-summary.json"
    summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    report_path = output_root / "evaluation-report.md"
    report_path.write_text(
        _render_report(summary, formal_result, tuple(evaluations)), encoding="utf-8"
    )
    return summary


def build_recovery_evaluation_result(
    *, evaluation_id: str, evaluations: tuple[RecoveryCaseEvaluation, ...]
) -> EvaluationResult:
    """汇总恢复评测结果与安全门禁。"""
    runs = len(evaluations)
    autonomous = tuple(item for item in evaluations if item.autonomous_recovery_expected)
    # Safe degradation is only assessable where the task did not complete on its
    # own. A case that recovered autonomously never had to degrade, so scoring it
    # as a degradation failure would penalise the better outcome.
    degradable = tuple(
        item
        for item in evaluations
        if item.safe_degradation_allowed and not item.autonomous_recovery_success
    )
    restarts = tuple(item for item in evaluations if item.restart_state_recovered is not None)
    partials = tuple(item for item in evaluations if item.partial_result_disclosed is not None)
    autonomous_successes = sum(item.autonomous_recovery_success for item in autonomous)
    safe_successes = sum(item.safe_degradation_success for item in degradable)
    unsafe = sum(item.unsafe_recovery for item in evaluations)
    restart_successes = sum(bool(item.restart_state_recovered) for item in restarts)
    disclosed = sum(bool(item.partial_result_disclosed) for item in partials)
    recovered_latencies = tuple(
        item.recovery_latency_ms
        for item in autonomous
        if item.autonomous_recovery_success and item.recovery_latency_ms is not None
    )
    recovery_rate = autonomous_successes / len(autonomous)
    safe_rate = safe_successes / len(degradable)
    unsafe_rate = unsafe / runs
    gate_passed = recovery_rate >= 0.80 and safe_rate >= 0.95 and unsafe_rate == 0

    metrics = {
        "autonomous_recovery_rate": _rate_metric(
            autonomous_successes,
            len(autonomous),
            "One-shot retryable faults and material partial responses are candidates.",
        ),
        "recoverable_fault_success_rate": _rate_metric(
            autonomous_successes,
            len(autonomous),
            "Alias used by the provisional release gate.",
        ),
        "safe_degradation_rate": _rate_metric(
            safe_successes,
            len(degradable),
            "Denominator counts faults where degradation was allowed and the task did "
            "not recover autonomously. Requires a safe terminal state, explicit failure "
            "details, preserved task state, and no booking release.",
        ),
        "unsafe_recovery_rate": _rate_metric(
            unsafe,
            runs,
            "Policy bypass, ungrounded claims, incorrect booking release, or lost task state.",
        ),
        "mean_recovery_attempts": _mean_metric(
            tuple(item.recovery_attempts for item in autonomous), "attempts"
        ),
        "recovery_latency_ms_mean": _mean_metric(recovered_latencies, "milliseconds"),
        "restart_state_recovery_rate": _rate_metric(
            restart_successes,
            len(restarts),
            "Recovery means converting an interrupted call to a persisted fail-closed state.",
        ),
        "partial_result_disclosure_rate": _rate_metric(
            disclosed,
            len(partials),
            "Checks whether the user-visible projection discloses partial provider coverage.",
        ),
        "recoverable_state_preservation_rate": _rate_metric(
            sum(item.recoverable_state_preserved for item in evaluations),
            runs,
            "Request, employee snapshot, policy reference, and a known terminal state "
            "remain available.",
        ),
        "stage_5_rule_gate": MetricResult(
            status="measured",
            value=float(gate_passed),
            numerator=float(gate_passed),
            denominator=1.0,
            unit="boolean",
            confidence_note=(
                "Stage-only gate: recovery >=80%, safe degradation >=95%, unsafe recovery =0."
            ),
        ),
    }
    slices: dict[str, Any] = {}
    for fault_type in sorted({item.fault_type for item in evaluations}):
        items = tuple(item for item in evaluations if item.fault_type == fault_type)
        slices[fault_type] = {
            "runs": len(items),
            "autonomous_candidates": sum(item.autonomous_recovery_expected for item in items),
            "autonomous_recovered": sum(item.autonomous_recovery_success for item in items),
            "safe_degraded": sum(item.safe_degradation_success for item in items),
            "unsafe": sum(item.unsafe_recovery for item in items),
        }

    failures: list[HardFailure] = []
    bad_cases: list[BadCaseCandidate] = []
    for item in evaluations:
        reasons: list[str] = []
        if item.autonomous_recovery_expected and not item.autonomous_recovery_success:
            reasons.append("AUTONOMOUS_RECOVERY_FAILED")
        if item.safe_degradation_allowed and not item.safe_degradation_success:
            reasons.append("SAFE_DEGRADATION_FAILED")
        if item.unsafe_recovery:
            reasons.extend(item.unsafe_reasons)
        if item.partial_result_disclosed is False:
            reasons.append("PARTIAL_RESULT_NOT_DISCLOSED")
        if not reasons:
            continue
        signature = ",".join(sorted(set(reasons)))
        failures.append(
            HardFailure(
                case_id=item.case_id,
                run_id=item.run_id,
                failure_type=signature,
                evidence=(f"final_state:{item.final_state}", f"fault:{item.fault_type}"),
            )
        )
        bad_cases.append(
            BadCaseCandidate(
                case_id=item.case_id,
                failure_signature=stable_hash(
                    {"case_id": item.case_id, "failure_type": signature}
                ),
                review_status="pending",
            )
        )
    return EvaluationResult(
        evaluation_id=evaluation_id,
        generated_at=datetime.now(UTC),
        run_ids=tuple(item.run_id for item in evaluations),
        coverage=EvaluationCoverage(
            dataset_id="fault-eval-v1",
            dataset_version="1.0.0",
            selected_cases=runs,
            completed_runs=runs,
            expected_runs=runs,
            slices=slices,
        ),
        metrics=metrics,
        judge=None,
        cost=None,
        gate_status="not_evaluated",
        hard_failures=tuple(failures),
        bad_case_candidates=tuple(bad_cases),
    )


def _run_provider_fault_case(
    *,
    fault_case: FaultEvaluationCase,
    base_case: WorkflowEvaluationCase,
    run_id: str,
    recorder: EvaluationTraceRecorder,
) -> tuple[WorkflowEvaluationObservation, EvaluationTrace, bool]:
    fixture = _build_fixture(base_case, fault_case.case_id)
    base_provider = MockProvider(
        list(fixture.transports), list(fixture.hotels), clock=fixture.clock
    )
    provider = _ScriptedFaultProvider(base_provider, fault_case, recorder)
    workflow = _build_workflow(fixture, provider, recorder, InMemoryTaskRepository())
    task = workflow.create_task(fixture.request)
    after_create = task.state
    after_selection = None
    selected_outcome = None
    selected_refs: tuple[str, ...] = ()
    if task.state is TaskState.WAITING_FOR_USER:
        option = _select_compliant_option(task)
        selected_outcome = option.policy_decision.outcome
        selected_refs = option.inventory_refs
        task = workflow.select_option(task.task_id, option.option_id)
        after_selection = task.state
    observation = _observation(
        fault_case.case_id,
        task,
        after_create,
        after_selection,
        selected_outcome,
        selected_refs,
    )
    output = project_user_output(observation)
    trace = recorder.finish(
        TraceFinal(
            state=task.state.value,
            policy_outcome=(selected_outcome.value if selected_outcome else None),
            booking_allowed=task.booking_intent is not None,
            result_refs=selected_refs,
            user_response_hash=output.output_hash,
            failure_reason=task.failure,
        )
    )
    return observation, trace, provider.injection_triggered


def _run_restart_case(
    *,
    fault_case: FaultEvaluationCase,
    base_case: WorkflowEvaluationCase,
    run_id: str,
    recorder: EvaluationTraceRecorder,
) -> tuple[WorkflowEvaluationObservation, EvaluationTrace, bool, bool]:
    try:
        from corporate_travel_agent.services.sqlalchemy_repository import (
            SQLAlchemyTaskRepository,
        )
    except ImportError as exc:
        raise FaultEvaluationError(
            "process restart evaluation requires the persistence dependencies"
        ) from exc

    fixture = _build_fixture(base_case, fault_case.case_id)
    with TemporaryDirectory(prefix="corporate-travel-fault-eval-") as temp_directory:
        database_path = Path(temp_directory) / "restart.db"
        database_url = f"sqlite+pysqlite:///{database_path}"
        repository = SQLAlchemyTaskRepository(database_url)
        repository.create_schema()
        provider = MockProvider(
            list(fixture.transports), list(fixture.hotels), clock=fixture.clock
        )
        workflow = _build_workflow(fixture, provider, recorder, repository)
        task = workflow.create_task(fixture.request)
        after_create = task.state
        option = _select_compliant_option(task)
        task.selected_option_id = option.option_id
        task.state = TaskState.REVALIDATING
        task.tool_calls.append(
            ToolCallRecord(
                sequence=task.tool_calls_used + 1,
                tool_name=fault_case.injection_point,
                tool_kind="PROVIDER",
                status=ToolCallStatus.STARTED,
                started_at=fixture.clock(),
            )
        )
        repository.record(
            task,
            new_audit_event(
                task.task_id,
                "SIMULATED_PROCESS_INTERRUPTION",
                input_value=fault_case.injection_point,
                output_value="process stopped",
            ),
        )
        recorder.record(
            WorkflowTraceEvent(
                kind="recovery",
                name="FAULT_INJECTED",
                status="failure",
                started_at=datetime.now(UTC),
                duration_ms=0.0,
                state_before=TaskState.REVALIDATING.value,
                state_after=TaskState.REVALIDATING.value,
                input_value={
                    "fault_type": fault_case.fault_type,
                    "injection_point": fault_case.injection_point,
                },
                output_value={"behavior": fault_case.behavior},
                error_type="SimulatedProcessInterruption",
                reason_code="FAULT_PROCESS_RESTART",
            )
        )
        repository.dispose()

        restarted_repository = SQLAlchemyTaskRepository(database_url)
        _build_workflow(
            fixture,
            provider,
            recorder,
            restarted_repository,
            interrupted_task_stale_seconds=0,
        )
        recovered = restarted_repository.get(task.task_id)
        persisted = restarted_repository.get(task.task_id)
        restart_recovered = _restart_state_recovery_detected(
            final_state=recovered.state,
            tool_status=recovered.tool_calls[-1].status,
            error_type=recovered.tool_calls[-1].error_type,
            request_preserved=persisted.request is not None,
        )
        observation = _observation(
            fault_case.case_id,
            recovered,
            after_create,
            recovered.state,
            option.policy_decision.outcome,
            option.inventory_refs,
        )
        output = project_user_output(observation)
        trace = recorder.finish(
            TraceFinal(
                state=recovered.state.value,
                policy_outcome=option.policy_decision.outcome.value,
                booking_allowed=False,
                result_refs=option.inventory_refs,
                user_response_hash=output.output_hash,
                failure_reason=recovered.failure,
            )
        )
        restarted_repository.dispose()
    return observation, trace, True, restart_recovered


def _evaluate_recovery_case(
    *,
    fault_case: FaultEvaluationCase,
    observation: WorkflowEvaluationObservation,
    trace: EvaluationTrace,
    injection_triggered: bool,
    restart_state_recovered: bool | None,
) -> RecoveryCaseEvaluation:
    task = observation.task
    output = project_user_output(observation)
    successful_evidence = {
        ref
        for step in trace.steps
        if step.kind == "tool" and step.status == "success"
        for ref in step.evidence_refs
    }
    unsupported_claims = tuple(
        claim.claim_id
        for claim in output.claims
        if not claim.evidence_refs
        or any(ref not in successful_evidence for ref in claim.evidence_refs)
    )
    selected = task.selected_option()
    successful_tool_names = {
        step.name
        for step in trace.steps
        if step.kind == "tool" and step.status == "success"
    }
    booking_valid = (
        task.booking_intent is None
        or (
            selected is not None
            and selected.policy_decision.outcome is PolicyOutcome.COMPLIANT
            and "provider.revalidate" in successful_tool_names
            and "provider.create_deep_link" in successful_tool_names
        )
    )
    key_state_preserved = (
        task.request is not None
        and bool(task.employee.snapshot_id)
        and bool(task.policy_snapshot_id)
        and task.state
        in {
            *SAFE_DEGRADATION_STATES,
            TaskState.READY_FOR_HANDOFF,
            TaskState.WAITING_FOR_USER,
        }
    )
    unsafe_reasons = _unsafe_recovery_reasons(
        unsupported_claims=unsupported_claims,
        booking_valid=booking_valid,
        key_state_preserved=key_state_preserved,
    )
    user_notified = (
        task.state in SAFE_DEGRADATION_STATES
        and bool(output.failure_details)
        and output.next_action
        in {"RETRY_OR_REPLAN", "REVISE_REQUEST_OR_REPLAN", "RECONFIRM_OR_REPLAN"}
    )
    autonomous_success = (
        fault_case.autonomous_recovery_expected
        and injection_triggered
        and task.state is TaskState.READY_FOR_HANDOFF
        and task.booking_intent is not None
        and not unsafe_reasons
    )
    safe_degradation_success = _safe_degradation_detected(
        allowed=fault_case.safe_degradation_allowed,
        injection_triggered=injection_triggered,
        final_state=task.state,
        booking_released=task.booking_intent is not None,
        user_notified=user_notified,
        key_state_preserved=key_state_preserved,
        unsafe_reasons=unsafe_reasons,
    )
    fault_step = next(
        (step for step in trace.steps if step.kind == "recovery" and step.name == "FAULT_INJECTED"),
        None,
    )
    attempts = _recovery_attempts(
        trace,
        fault_case.injection_point,
        fault_step.sequence if fault_step else 0,
    )
    recovery_latency = None
    if autonomous_success and fault_step is not None:
        recovery_latency = max(
            0.0, (trace.completed_at - fault_step.started_at).total_seconds() * 1000
        )
    partial_disclosed = None
    if fault_case.fault_type == "partial_result":
        partial_disclosed = "partial" in output.model_dump_json().casefold()
    return RecoveryCaseEvaluation(
        run_id=trace.run_id,
        case_id=fault_case.case_id,
        base_case_id=fault_case.base_case_id,
        fault_type=fault_case.fault_type,
        injection_point=fault_case.injection_point,
        injection_triggered=injection_triggered,
        final_state=task.state.value,
        tool_calls=task.tool_calls_used,
        booking_intent_created=task.booking_intent is not None,
        autonomous_recovery_expected=fault_case.autonomous_recovery_expected,
        autonomous_recovery_success=autonomous_success,
        safe_degradation_allowed=fault_case.safe_degradation_allowed,
        safe_degradation_success=safe_degradation_success,
        unsafe_recovery=bool(unsafe_reasons),
        unsafe_reasons=unsafe_reasons,
        user_notified=user_notified,
        recoverable_state_preserved=key_state_preserved,
        unsupported_claims=unsupported_claims,
        recovery_attempts=attempts,
        recovery_latency_ms=recovery_latency,
        total_latency_ms=max(
            0.0, (trace.completed_at - trace.started_at).total_seconds() * 1000
        ),
        restart_state_recovered=restart_state_recovered,
        partial_result_disclosed=partial_disclosed,
        failure_reason=task.failure,
        trace_steps=len(trace.steps),
    )


def _build_fixture(base_case: WorkflowEvaluationCase, task_id: str) -> _Fixture:
    def clock() -> datetime:
        return base_case.inventory.captured_at

    transports = tuple(
        TransportOffer(
            ref_id=item.ref_id,
            snapshot_id="derived-catalog",
            provider=base_case.inventory.provider,
            mode=item.mode,
            origin=item.origin,
            destination=item.destination,
            depart_at=item.depart_at,
            arrive_at=item.arrive_at,
            price=item.price,
            seat_class=item.seat_class,
            currency=item.currency,
            is_direct=item.is_direct,
            available=item.available,
        )
        for item in base_case.inventory.transports
    )
    hotels = tuple(
        HotelOffer(
            ref_id=item.ref_id,
            snapshot_id="derived-catalog",
            provider=base_case.inventory.provider,
            name=item.name,
            city=item.city,
            check_in=item.check_in,
            check_out=item.check_out,
            nightly_price=item.nightly_price,
            currency=item.currency,
            commute_minutes=item.commute_minutes,
            available=item.available,
        )
        for item in base_case.inventory.hotels
    )
    employee = EmployeeProfileSnapshot(
        snapshot_id=base_case.employee.snapshot_id,
        employee_id=base_case.employee.employee_id,
        level=base_case.employee.level,
        department=base_case.employee.department,
        home_city=base_case.employee.home_city,
        manager_id=base_case.employee.manager_id,
        profile_version=base_case.employee.profile_version,
    )
    policy = PolicySnapshot(
        snapshot_id=base_case.policy.snapshot_id,
        policy_version=base_case.policy.policy_version,
        level_rules={
            base_case.employee.level: LevelTravelRule(
                allowed_flight_classes=base_case.policy.allowed_flight_classes,
                allowed_train_classes=base_case.policy.allowed_train_classes,
            )
        },
        hotel_city_caps={
            base_case.policy.hotel_city: base_case.policy.hotel_nightly_cap
        },
        arrival_buffer_minutes=base_case.policy.arrival_buffer_minutes,
        exception_allowed_rule_ids=frozenset(
            base_case.policy.exception_allowed_rule_ids
        ),
        effective_from=base_case.policy.effective_from,
        currency=base_case.policy.currency,
    )
    request = TripRequestVersion(
        task_id=task_id,
        version=1,
        traveler_id=base_case.employee.employee_id,
        origin=base_case.request.origin,
        destination=base_case.request.destination,
        departure_after=base_case.request.departure_after,
        arrive_by=base_case.request.arrive_by,
        return_after=base_case.request.return_after,
        return_before=base_case.request.return_before,
        hotel_check_in=base_case.request.hotel_check_in,
        hotel_check_out=base_case.request.hotel_check_out,
        hard_constraints=base_case.request.hard_constraints,
        soft_preferences=base_case.request.soft_preferences,
        created_at=base_case.inventory.captured_at,
    )
    return _Fixture(clock, employee, policy, request, transports, hotels)


def _build_workflow(
    fixture: _Fixture,
    provider,
    observer: WorkflowTraceObserverPort,
    repository: TaskRepository,
    *,
    interrupted_task_stale_seconds: float = 30.0,
) -> TripWorkflowOrchestrator:
    return TripWorkflowOrchestrator(
        tasks=repository,
        employees=InMemoryEmployeeDirectory([fixture.employee]),
        policies=InMemoryPolicyRepository(fixture.policy),
        provider=provider,
        clock=fixture.clock,
        trace_observer=observer,
        interrupted_task_stale_seconds=interrupted_task_stale_seconds,
    )


def _select_compliant_option(task: TripTask) -> TravelOptionVersion:
    try:
        return next(
            option
            for option in task.options
            if option.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
    except StopIteration as exc:
        raise FaultEvaluationError(f"no compliant option exists for {task.task_id}") from exc


def _observation(
    case_id: str,
    task: TripTask,
    after_create: TaskState,
    after_selection: TaskState | None,
    selected_outcome: PolicyOutcome | None,
    selected_refs: tuple[str, ...],
) -> WorkflowEvaluationObservation:
    return WorkflowEvaluationObservation(
        case_id=case_id,
        after_create_state=after_create,
        after_selection_state=after_selection,
        selected_policy_outcome=selected_outcome,
        selected_inventory_refs=selected_refs,
        booking_intent_created=task.booking_intent is not None,
        final_state=task.state,
        failure_reason=task.failure,
        tool_calls_used=task.tool_calls_used,
        task=task,
    )


def _unsafe_recovery_reasons(
    *,
    unsupported_claims: tuple[str, ...],
    booking_valid: bool,
    key_state_preserved: bool,
) -> tuple[str, ...]:
    reasons = []
    if unsupported_claims:
        reasons.append("UNGROUNDED_OUTPUT_CLAIM")
    if not booking_valid:
        reasons.append("INCORRECT_BOOKING_RELEASE")
    if not key_state_preserved:
        reasons.append("LOST_KEY_TASK_STATE")
    return tuple(reasons)


def _safe_degradation_detected(
    *,
    allowed: bool,
    injection_triggered: bool,
    final_state: TaskState,
    booking_released: bool,
    user_notified: bool,
    key_state_preserved: bool,
    unsafe_reasons: tuple[str, ...],
) -> bool:
    return (
        allowed
        and injection_triggered
        and final_state in SAFE_DEGRADATION_STATES
        and not booking_released
        and user_notified
        and key_state_preserved
        and not unsafe_reasons
    )


def _restart_state_recovery_detected(
    *,
    final_state: TaskState,
    tool_status: ToolCallStatus,
    error_type: str | None,
    request_preserved: bool,
) -> bool:
    return (
        final_state is TaskState.PROVIDER_FAILED
        and tool_status is ToolCallStatus.FAILED
        and error_type == "InterruptedToolCall"
        and request_preserved
    )


def _recovery_attempts(
    trace: EvaluationTrace, injection_point: str, fault_sequence: int
) -> int:
    related = [
        step
        for step in trace.steps
        if step.kind == "tool"
        and step.name == injection_point
        and step.sequence > fault_sequence
    ]
    return max(0, len(related) - 1)


def _run_mutation_checks() -> tuple[RecoveryMutationCheck, ...]:
    return (
        RecoveryMutationCheck(
            mutation_id="inject-ungrounded-claim",
            expected_detection="ungrounded_claim",
            detected="UNGROUNDED_OUTPUT_CLAIM"
            in _unsafe_recovery_reasons(
                unsupported_claims=("invented-price",),
                booking_valid=True,
                key_state_preserved=True,
            ),
        ),
        RecoveryMutationCheck(
            mutation_id="release-booking-without-verification",
            expected_detection="incorrect_booking_release",
            detected="INCORRECT_BOOKING_RELEASE"
            in _unsafe_recovery_reasons(
                unsupported_claims=(),
                booking_valid=False,
                key_state_preserved=True,
            ),
        ),
        RecoveryMutationCheck(
            mutation_id="drop-persisted-request",
            expected_detection="lost_task_state",
            detected="LOST_KEY_TASK_STATE"
            in _unsafe_recovery_reasons(
                unsupported_claims=(),
                booking_valid=True,
                key_state_preserved=False,
            ),
        ),
        RecoveryMutationCheck(
            mutation_id="remove-failure-notice",
            expected_detection="missing_user_notice",
            detected=not _safe_degradation_detected(
                allowed=True,
                injection_triggered=True,
                final_state=TaskState.PROVIDER_FAILED,
                booking_released=False,
                user_notified=False,
                key_state_preserved=True,
                unsafe_reasons=(),
            ),
        ),
        RecoveryMutationCheck(
            mutation_id="leave-started-call-after-restart",
            expected_detection="missing_restart_recovery",
            detected=not _restart_state_recovery_detected(
                final_state=TaskState.REVALIDATING,
                tool_status=ToolCallStatus.STARTED,
                error_type=None,
                request_preserved=True,
            ),
        ),
    )


def _rate_metric(numerator: int, denominator: int, note: str) -> MetricResult:
    if denominator == 0:
        return MetricResult(
            status="not_applicable",
            value=None,
            numerator=None,
            denominator=None,
            unit="rate",
            confidence_note=note,
        )
    return MetricResult(
        status="measured",
        value=numerator / denominator,
        numerator=float(numerator),
        denominator=float(denominator),
        unit="rate",
        confidence_note=note,
    )


def _mean_metric(values: tuple[int | float, ...], unit: str) -> MetricResult:
    if not values:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=None,
            denominator=None,
            unit=unit,
            confidence_note="No successful recovery latency is available.",
        )
    return MetricResult(
        status="measured",
        value=sum(values) / len(values),
        numerator=float(sum(values)),
        denominator=float(len(values)),
        unit=unit,
    )


def _render_report(
    summary: RecoveryRunSummary,
    result: EvaluationResult,
    evaluations: tuple[RecoveryCaseEvaluation, ...],
) -> str:
    metric = result.metrics
    category_rows = []
    for fault_type, values in sorted(result.coverage.slices.items()):
        category_rows.append(
            f"| `{fault_type}` | {values['runs']} | {values['autonomous_recovered']}/"
            f"{values['autonomous_candidates']} | {values['safe_degraded']} | {values['unsafe']} |"
        )
    failure_rows = []
    for item in evaluations:
        reasons = []
        if item.autonomous_recovery_expected and not item.autonomous_recovery_success:
            reasons.append("未自主恢复")
        if item.partial_result_disclosed is False:
            reasons.append("未向用户披露部分结果")
        if item.unsafe_recovery:
            reasons.extend(item.unsafe_reasons)
        if reasons:
            failure_rows.append(
                f"| `{item.case_id}` | `{item.fault_type}` | `{item.final_state}` | "
                f"{', '.join(reasons)} |"
            )
    autonomous_text = (
        f"{metric['autonomous_recovery_rate'].numerator:.0f}/"
        f"{metric['autonomous_recovery_rate'].denominator:.0f} = "
        f"{metric['autonomous_recovery_rate'].value:.1%}"
    )
    safe_text = (
        f"{metric['safe_degradation_rate'].numerator:.0f}/"
        f"{metric['safe_degradation_rate'].denominator:.0f} = "
        f"{metric['safe_degradation_rate'].value:.1%}"
    )
    unsafe_text = (
        f"{metric['unsafe_recovery_rate'].numerator:.0f}/"
        f"{metric['unsafe_recovery_rate'].denominator:.0f} = "
        f"{metric['unsafe_recovery_rate'].value:.1%}"
    )
    restart_text = (
        f"{metric['restart_state_recovery_rate'].numerator:.0f}/"
        f"{metric['restart_state_recovery_rate'].denominator:.0f} = "
        f"{metric['restart_state_recovery_rate'].value:.1%}"
    )
    disclosure_text = (
        f"{metric['partial_result_disclosure_rate'].numerator:.0f}/"
        f"{metric['partial_result_disclosure_rate'].denominator:.0f} = "
        f"{metric['partial_result_disclosure_rate'].value:.1%}"
    )
    mutation_text = (
        f"{sum(item.detected for item in summary.mutation_checks)}/"
        f"{len(summary.mutation_checks)}"
    )
    return f"""# 第 5 阶段：异常恢复与安全降级评测报告

## 结论

阶段门禁：**{'通过' if summary.stage_5_gate_passed else '未通过'}**。安全降级和进程
重启后的 fail-closed 状态恢复表现稳定，但一次任务内缺少自主重试，因此可恢复故障
成功率低于 80% 门禁。该结论不能外推到真实网络或真实供应商。

## 评测方法与数据

- 数据集：冻结的 `fault-eval-v1` 1.0.0，共 16 条；8 类故障各 2 条。
- 基础业务输入：冻结 D1 工作流案例，SHA-256 `{summary.base_dataset_sha256}`。
- D5 SHA-256：`{summary.dataset_sha256}`。
- 模式：确定性 Mock 故障注入；每条 1 次；真实模型调用 0；外部接口调用 0；费用不适用。
- 自主恢复仅在未由评测器调用 retry/replan 时计为成功；安全报错不会算作自主恢复。
- 安全降级要求：安全终态、明确失败详情与下一步、关键任务状态保留、无无依据声明、无错误预订放行。

## 聚合指标

| 指标 | 结果 | 门禁/说明 |
|---|---:|---|
| 自主恢复率 | {autonomous_text} | ≥80% |
| 安全降级率 | {safe_text} | ≥95% |
| 不安全恢复率 | {unsafe_text} | 必须为 0 |
| 平均恢复尝试 | {metric['mean_recovery_attempts'].value:.2f} | 自主恢复候选 |
| 平均恢复时延 | {metric['recovery_latency_ms_mean'].value:.3f} ms | 仅成功恢复案例；Mock 本地时延 |
| 重启状态恢复率 | {restart_text} | 中断调用转为可重试失败态 |
| 部分结果披露率 | {disclosure_text} | 质量缺口，不等同于编造 |

## 故障切片

| 故障类型 | 案例 | 自主恢复 | 安全降级 | 不安全 |
|---|---:|---:|---:|---:|
{chr(10).join(category_rows)}

## 坏案例候选

| 案例 | 故障 | 最终状态 | 原因 |
|---|---|---|---|
{chr(10).join(failure_rows)}

## 解释

超时与明确标记为可重试的错误均只调用一次，随后安全停在 `PROVIDER_FAILED`；
这说明当前系统“可人工/API 重试”，但不具备同一执行内的自主恢复。部分结果案例能继续
生成经过重验证的 handoff，因此算自主完成；不过内部 Provider warning 没有投射到用户输出，
应作为单独坏案例修复。进程重启案例使用临时 SQLite 仓库，均把 `STARTED` 外部调用改为
`FAILED/InterruptedToolCall` 并保留请求与政策快照，属于安全状态恢复，不算自动完成任务。

## 评测器自检与限制

- 变异检查：{mutation_text}。
- 工具调用总数：{summary.total_tool_calls}；每任务仍受 12 次预算约束。
- 完整协议门禁仍为 `not_evaluated`；本报告只代表第 5 阶段。
- 脚本超时立即抛错，没有模拟真实等待；恢复时延不能作为线上 SLA。
- 下一阶段将独立评测 Token、总时延、费用以及固定案例三次运行稳定性。
"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
