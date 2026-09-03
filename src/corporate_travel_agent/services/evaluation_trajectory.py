"""评测什么：工具轨迹合规——非法工具、参数幻觉、影子调用与期望路径。"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_dataset import (
    WorkflowEvaluationCase,
    load_evaluation_dataset,
)
from corporate_travel_agent.services.evaluation_quality import (
    BadCaseCandidate,
    EvaluationCoverage,
    EvaluationResult,
    HardFailure,
    MetricResult,
    OutputClaim,
    WorkflowCaseEvaluation,
)
from corporate_travel_agent.services.evaluation_trace import EvaluationTrace, TraceStep

TRAJECTORY_EVALUATOR_VERSION: Final = "trajectory-evaluator-v1"
SHA256_PATTERN = r"^[a-f0-9]{64}$"

HallucinationType = Literal["tool", "parameter_schema", "parameter_unsupported", "shadow"]
MutationDetection = Literal[
    "tool",
    "parameter_schema",
    "parameter_unsupported",
    "shadow",
    "trajectory_order",
]
ParameterType = Literal[
    "string",
    "datetime",
    "date",
    "string_array",
    "positive_integer",
    "object",
]


class TrajectoryEvaluationError(RuntimeError):
    """轨迹评测失败。"""
    pass


class TrajectoryModel(BaseModel):
    """轨迹评测模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolContract(TrajectoryModel):
    """工具契约（允许参数与约束）。"""
    name: str = Field(min_length=1)
    required_parameters: tuple[str, ...]
    parameter_types: dict[str, ParameterType]
    allowed_states: tuple[str, ...]
    provenance: str = Field(min_length=1)

    @model_validator(mode="after")
    def parameter_contract_is_complete(self) -> ToolContract:
        if set(self.required_parameters) != set(self.parameter_types):
            raise ValueError("required parameters and parameter types must match exactly")
        return self


class ToolRegistry(TrajectoryModel):
    """工具注册表。"""
    schema_version: Literal[1]
    registry_id: Literal[
        "corporate-travel-tool-registry-v1",
        "corporate-travel-tool-registry-v2",
    ]
    tool_choice_exposure: Literal["orchestrator_controlled"]
    tools: tuple[ToolContract, ...]

    @model_validator(mode="after")
    def unique_tool_names(self) -> ToolRegistry:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool registry names must be unique")
        return self


class TrajectoryCheck(TrajectoryModel):
    """单项轨迹检查结果。"""
    check_id: str
    passed: bool
    expected: Any
    actual: Any
    evidence: tuple[str, ...] = ()


class HallucinationFinding(TrajectoryModel):
    """幻觉/违规发现。"""
    finding_type: HallucinationType
    subject_id: str
    tool_name: str | None
    step_sequence: int | None = Field(default=None, ge=1)
    reason_code: str
    evidence: tuple[str, ...] = ()


class TrajectoryCaseEvaluation(TrajectoryModel):
    """单用例轨迹评估。"""
    run_id: str
    case_id: str
    scenario: str
    tool_choice_exposure: str
    tool_calls: int = Field(ge=0)
    verifiable_claims: int = Field(ge=0)
    required_checks: tuple[TrajectoryCheck, ...]
    forbidden_checks: tuple[TrajectoryCheck, ...]
    order_checks: tuple[TrajectoryCheck, ...]
    state_checks: tuple[TrajectoryCheck, ...]
    findings: tuple[HallucinationFinding, ...]
    trajectory_pass: bool


class MutationCheck(TrajectoryModel):
    """轨迹评测器突变检查。"""
    mutation_id: str
    expected_detection: MutationDetection
    detected: bool


class TrajectoryRunSummary(TrajectoryModel):
    """轨迹评测运行汇总。"""
    schema_version: Literal[1] = 1
    evaluation_id: str
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    evaluator_version: Literal["trajectory-evaluator-v1"] = TRAJECTORY_EVALUATOR_VERSION
    generated_at: datetime
    evaluation_mode: Literal["deterministic_mock_offline_replay"]
    dataset_id: str
    dataset_version: str
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    selected_cases: int = Field(ge=1)
    completed_runs: int = Field(ge=0)
    attempts_per_case: int = Field(ge=1)
    source_trace_file: str
    source_trace_sha256: str = Field(pattern=SHA256_PATTERN)
    source_case_results_file: str
    source_case_results_sha256: str = Field(pattern=SHA256_PATTERN)
    tool_registry_file: str
    tool_registry_sha256: str = Field(pattern=SHA256_PATTERN)
    trajectory_case_results_file: str
    trajectory_case_results_sha256: str = Field(pattern=SHA256_PATTERN)
    evaluation_result_file: str
    evaluation_result_sha256: str = Field(pattern=SHA256_PATTERN)
    total_tool_calls: int = Field(ge=0)
    total_verifiable_claims: int = Field(ge=0)
    required_tool_patterns: int = Field(ge=0)
    stage_3_gate_passed: bool
    full_protocol_gate: Literal["not_evaluated"] = "not_evaluated"
    mutation_checks: tuple[MutationCheck, ...] = ()
    limitations: tuple[str, ...]


def load_tool_registry(path: str | Path) -> ToolRegistry:
    """加载工具契约注册表。"""
    return ToolRegistry.model_validate_json(Path(path).read_text(encoding="utf-8"))


def evaluate_trajectory_case(
    *,
    case: WorkflowEvaluationCase,
    trace: EvaluationTrace,
    case_result: WorkflowCaseEvaluation,
    registry: ToolRegistry,
) -> TrajectoryCaseEvaluation:
    """评估单条轨迹是否符合工具契约与期望路径。"""
    if case.case_id != trace.case_id or trace.case_id != case_result.case_id:
        raise TrajectoryEvaluationError("case, trace, and result IDs do not match")
    if trace.run_id != case_result.run_id:
        raise TrajectoryEvaluationError("trace and result run IDs do not match")

    contracts = {tool.name: tool for tool in registry.tools}
    tool_steps = tuple(step for step in trace.steps if step.kind == "tool")
    expected_tools = expected_tool_path(
        case, model_in_the_loop=trace_has_model_in_the_loop(trace)
    )
    required_checks = tuple(
        TrajectoryCheck(
            check_id=f"required:{name}",
            passed=any(step.name == name for step in tool_steps),
            expected="at_least_once",
            actual=sum(step.name == name for step in tool_steps),
            evidence=tuple(
                f"step:{step.sequence}" for step in tool_steps if step.name == name
            ),
        )
        for name in expected_tools
    )
    forbidden_names = tuple(name for name in contracts if name not in expected_tools)
    forbidden_checks = tuple(
        TrajectoryCheck(
            check_id=f"forbidden:{name}",
            passed=not any(step.name == name for step in tool_steps),
            expected=0,
            actual=sum(step.name == name for step in tool_steps),
            evidence=tuple(
                f"step:{step.sequence}" for step in tool_steps if step.name == name
            ),
        )
        for name in forbidden_names
    )
    order_checks = _order_checks(expected_tools, tool_steps)

    findings: list[HallucinationFinding] = []
    state_checks: list[TrajectoryCheck] = []
    for step in tool_steps:
        if step.name not in contracts:
            findings.append(
                HallucinationFinding(
                    finding_type="tool",
                    subject_id=f"tool-step:{step.sequence}",
                    tool_name=step.name,
                    step_sequence=step.sequence,
                    reason_code="TOOL_NOT_IN_REGISTRY",
                    evidence=(f"step:{step.sequence}",),
                )
            )
            continue
        contract = contracts[step.name]
        state_ok = step.state_before in contract.allowed_states
        state_checks.append(
            TrajectoryCheck(
                check_id=f"allowed-state:{step.sequence}",
                passed=state_ok,
                expected=contract.allowed_states,
                actual=step.state_before,
                evidence=(f"step:{step.sequence}",),
            )
        )
        schema_reasons = _parameter_schema_reasons(step, contract)
        if schema_reasons:
            findings.append(
                HallucinationFinding(
                    finding_type="parameter_schema",
                    subject_id=f"tool-step:{step.sequence}",
                    tool_name=step.name,
                    step_sequence=step.sequence,
                    reason_code=";".join(schema_reasons),
                    evidence=(f"step:{step.sequence}",),
                )
            )
            continue
        unsupported = _unsupported_parameter_reasons(
            step=step,
            case=case,
            case_result=case_result,
            prior_steps=tuple(item for item in tool_steps if item.sequence < step.sequence),
        )
        if unsupported:
            findings.append(
                HallucinationFinding(
                    finding_type="parameter_unsupported",
                    subject_id=f"tool-step:{step.sequence}",
                    tool_name=step.name,
                    step_sequence=step.sequence,
                    reason_code=";".join(unsupported),
                    evidence=(f"step:{step.sequence}",),
                )
            )

    claims = case_result.user_output.claims
    for claim in claims:
        reason = _shadow_reason(claim, trace)
        if reason is not None:
            findings.append(
                HallucinationFinding(
                    finding_type="shadow",
                    subject_id=claim.claim_id,
                    tool_name=None,
                    step_sequence=None,
                    reason_code=reason,
                    evidence=claim.evidence_refs,
                )
            )

    trajectory_pass = all(
        check.passed
        for check in (*required_checks, *forbidden_checks, *order_checks, *state_checks)
    ) and not findings
    return TrajectoryCaseEvaluation(
        run_id=trace.run_id,
        case_id=case.case_id,
        scenario=case.scenario,
        tool_choice_exposure=trace.tool_choice_exposure,
        tool_calls=len(tool_steps),
        verifiable_claims=len(claims),
        required_checks=required_checks,
        forbidden_checks=forbidden_checks,
        order_checks=order_checks,
        state_checks=tuple(state_checks),
        findings=tuple(findings),
        trajectory_pass=trajectory_pass,
    )


def evaluate_trajectory_run(
    *,
    dataset_directory: str | Path,
    source_run_directory: str | Path,
    output_directory: str | Path,
    tool_registry_path: str | Path,
) -> TrajectoryRunSummary:
    """批量评估运行中的轨迹。"""
    dataset = load_evaluation_dataset(dataset_directory)
    source_root = Path(source_run_directory).expanduser().resolve()
    output_root = Path(output_directory).expanduser().resolve()
    registry_path = Path(tool_registry_path).expanduser().resolve()
    if output_root.exists():
        raise TrajectoryEvaluationError(f"Output directory already exists: {output_root}")

    trace_path = source_root / "traces.jsonl"
    case_result_path = source_root / "case-results.jsonl"
    trace_payload = trace_path.read_bytes()
    case_result_payload = case_result_path.read_bytes()
    registry_payload = registry_path.read_bytes()
    traces = tuple(
        EvaluationTrace.model_validate_json(line)
        for line in trace_payload.decode("utf-8").splitlines()
        if line
    )
    case_results = tuple(
        WorkflowCaseEvaluation.model_validate_json(line)
        for line in case_result_payload.decode("utf-8").splitlines()
        if line
    )
    registry = ToolRegistry.model_validate_json(registry_payload)
    cases = {case.case_id: case for case in dataset.workflow_cases}
    results_by_run = {result.run_id: result for result in case_results}
    if len(traces) != len(case_results) or len(results_by_run) != len(case_results):
        raise TrajectoryEvaluationError("source trace and case-result coverage does not match")
    if any(trace.case_id not in cases for trace in traces):
        raise TrajectoryEvaluationError("source run includes a case outside the frozen dataset")
    if any(
        trace.fingerprint.dataset_sha256 != dataset.manifest.workflow_cases.sha256
        for trace in traces
    ):
        raise TrajectoryEvaluationError("source trace dataset fingerprint does not match D1")

    evaluations = tuple(
        evaluate_trajectory_case(
            case=cases[trace.case_id],
            trace=trace,
            case_result=results_by_run[trace.run_id],
            registry=registry,
        )
        for trace in traces
    )
    evaluation_id = f"eval-{uuid4()}"
    formal_result = build_trajectory_evaluation_result(
        evaluation_id=evaluation_id,
        dataset_id=traces[0].fingerprint.dataset_id,
        dataset_version=dataset.manifest.dataset_version,
        evaluations=evaluations,
    )

    output_root.mkdir(parents=True)
    cases_output = output_root / "trajectory-case-results.jsonl"
    cases_payload = "".join(
        f"{item.model_dump_json()}\n" for item in evaluations
    ).encode("utf-8")
    cases_output.write_bytes(cases_payload)
    result_output = output_root / "trajectory-evaluation-result.json"
    result_payload = formal_result.model_dump_json(indent=2).encode("utf-8")
    result_output.write_bytes(result_payload)

    required_count = sum(len(item.required_checks) for item in evaluations)
    mutation_trace = next(
        (
            trace
            for trace in traces
            if cases[trace.case_id].scenario == "COMPLIANT"
        ),
        None,
    )
    summary = TrajectoryRunSummary(
        evaluation_id=evaluation_id,
        generated_at=datetime.now(UTC),
        evaluation_mode="deterministic_mock_offline_replay",
        dataset_id=traces[0].fingerprint.dataset_id,
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.workflow_cases.sha256,
        selected_cases=len({item.case_id for item in evaluations}),
        completed_runs=len(evaluations),
        attempts_per_case=max(trace.attempt for trace in traces),
        source_trace_file=str(trace_path),
        source_trace_sha256=_sha256(trace_payload),
        source_case_results_file=str(case_result_path),
        source_case_results_sha256=_sha256(case_result_payload),
        tool_registry_file=str(registry_path),
        tool_registry_sha256=_sha256(registry_payload),
        trajectory_case_results_file=cases_output.name,
        trajectory_case_results_sha256=_sha256(cases_payload),
        evaluation_result_file=result_output.name,
        evaluation_result_sha256=_sha256(result_payload),
        total_tool_calls=sum(item.tool_calls for item in evaluations),
        total_verifiable_claims=sum(item.verifiable_claims for item in evaluations),
        required_tool_patterns=required_count,
        stage_3_gate_passed=bool(formal_result.metrics["stage_3_rule_gate"].value),
        mutation_checks=(
            run_evaluator_mutation_checks(
                case=cases[mutation_trace.case_id],
                trace=mutation_trace,
                case_result=results_by_run[mutation_trace.run_id],
                registry=registry,
            )
            if mutation_trace is not None
            else ()
        ),
        limitations=(
            "All inventory is deterministic MOCK data.",
            "Tool choice is orchestrator-controlled; zero observed tool hallucinations "
            "do not generalize to open model tool choice.",
            "The deterministic user-output projection exposes selected inventory, "
            "policy, and workflow claims only.",
            "Each case has one recorded attempt; real-model stability is not measured.",
            "No real model or external provider call was executed by this offline evaluation.",
        ),
    )
    (output_root / "trajectory-run-summary.json").write_text(
        summary.model_dump_json(indent=2), encoding="utf-8"
    )
    return summary


def build_trajectory_evaluation_result(
    *,
    evaluation_id: str,
    dataset_id: str,
    dataset_version: str,
    evaluations: tuple[TrajectoryCaseEvaluation, ...],
) -> EvaluationResult:
    """汇总轨迹评测结果。"""
    runs = len(evaluations)
    tool_calls = sum(item.tool_calls for item in evaluations)
    claims = sum(item.verifiable_claims for item in evaluations)
    required = tuple(check for item in evaluations for check in item.required_checks)
    forbidden_runs = sum(
        any(not check.passed for check in item.forbidden_checks)
        for item in evaluations
    )
    order_runs = sum(
        any(not check.passed for check in (*item.order_checks, *item.state_checks))
        for item in evaluations
    )
    trajectory_passes = sum(item.trajectory_pass for item in evaluations)
    findings = tuple(finding for item in evaluations for finding in item.findings)
    tool_findings = tuple(item for item in findings if item.finding_type == "tool")
    schema_findings = tuple(
        item for item in findings if item.finding_type == "parameter_schema"
    )
    unsupported_findings = tuple(
        item for item in findings if item.finding_type == "parameter_unsupported"
    )
    parameter_subjects = {
        (item.subject_id, item.tool_name)
        for item in (*schema_findings, *unsupported_findings)
    }
    shadow_findings = tuple(item for item in findings if item.finding_type == "shadow")
    tool_runs = _affected_runs(evaluations, {"tool"})
    parameter_runs = _affected_runs(
        evaluations, {"parameter_schema", "parameter_unsupported"}
    )
    shadow_runs = _affected_runs(evaluations, {"shadow"})

    gate_pass = (
        trajectory_passes == runs
        and not tool_findings
        and not parameter_subjects
        and not shadow_findings
    )
    metrics = {
        "trajectory_pass_rate": _rate(trajectory_passes, runs),
        "required_tool_coverage": _rate(sum(check.passed for check in required), len(required)),
        "forbidden_tool_call_rate": _rate(forbidden_runs, runs),
        "order_violation_rate": _rate(order_runs, runs),
        "tool_hallucination_call_rate": _rate(
            len(tool_findings),
            tool_calls,
            exposure_note="Tool choice is orchestrator-controlled in this run.",
        ),
        "tool_hallucination_run_rate": _rate(
            tool_runs,
            runs,
            exposure_note="Tool choice is orchestrator-controlled in this run.",
        ),
        "parameter_hallucination_call_rate": _rate(len(parameter_subjects), tool_calls),
        "parameter_hallucination_run_rate": _rate(parameter_runs, runs),
        "parameter_schema_error_rate": _rate(len(schema_findings), tool_calls),
        "parameter_unsupported_value_rate": _rate(len(unsupported_findings), tool_calls),
        "shadow_hallucination_claim_rate": _rate(len(shadow_findings), claims),
        "shadow_hallucination_run_rate": _rate(shadow_runs, runs),
        "stage_3_rule_gate": MetricResult(
            status="measured",
            value=float(gate_pass),
            numerator=float(gate_pass),
            denominator=1.0,
            unit="boolean",
            confidence_note="Stage-only rule gate; the full protocol gate remains not evaluated.",
        ),
    }
    scenario_names = sorted({item.scenario for item in evaluations})
    slices: dict[str, Any] = {}
    for scenario in scenario_names:
        items = tuple(item for item in evaluations if item.scenario == scenario)
        slices[scenario] = {
            "runs": len(items),
            "trajectory_passed": sum(item.trajectory_pass for item in items),
            "findings": sum(len(item.findings) for item in items),
        }
        metrics[f"trajectory_pass_rate.scenario.{scenario}"] = _rate(
            sum(item.trajectory_pass for item in items), len(items)
        )

    hard_failures: list[HardFailure] = []
    bad_cases: list[BadCaseCandidate] = []
    for item in evaluations:
        failed_checks = tuple(
            check
            for check in (
                *item.required_checks,
                *item.forbidden_checks,
                *item.order_checks,
                *item.state_checks,
            )
            if not check.passed
        )
        if failed_checks or item.findings:
            failure_types = tuple(
                [check.check_id for check in failed_checks]
                + [finding.finding_type for finding in item.findings]
            )
            signature = ",".join(failure_types)
            hard_failures.append(
                HardFailure(
                    case_id=item.case_id,
                    run_id=item.run_id,
                    failure_type=signature,
                    evidence=tuple(
                        value
                        for check in failed_checks
                        for value in check.evidence
                    )
                    + tuple(value for finding in item.findings for value in finding.evidence),
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
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            selected_cases=len({item.case_id for item in evaluations}),
            completed_runs=runs,
            expected_runs=runs,
            slices=slices,
        ),
        metrics=metrics,
        judge=None,
        cost=None,
        gate_status="not_evaluated",
        hard_failures=tuple(hard_failures),
        bad_case_candidates=tuple(bad_cases),
    )


def run_evaluator_mutation_checks(
    *,
    case: WorkflowEvaluationCase,
    trace: EvaluationTrace,
    case_result: WorkflowCaseEvaluation,
    registry: ToolRegistry,
) -> tuple[MutationCheck, ...]:
    """运行轨迹评测器突变检查。"""

    tool_steps = tuple(step for step in trace.steps if step.kind == "tool")
    first_tool = tool_steps[0]

    unknown_tool_trace = _replace_trace_step(
        trace,
        first_tool,
        first_tool.model_copy(update={"name": "provider.nonexistent_tool"}),
    )
    unknown_tool_result = evaluate_trajectory_case(
        case=case,
        trace=unknown_tool_trace,
        case_result=case_result,
        registry=registry,
    )

    missing_payload = dict(first_tool.redacted_input or {})
    missing_payload.pop("origin", None)
    schema_trace = _replace_trace_step(
        trace,
        first_tool,
        first_tool.model_copy(update={"redacted_input": missing_payload}),
    )
    schema_result = evaluate_trajectory_case(
        case=case,
        trace=schema_trace,
        case_result=case_result,
        registry=registry,
    )

    unsupported_payload = dict(first_tool.redacted_input or {})
    unsupported_payload["origin"] = "UNSUPPORTED-SYNTHETIC-CITY"
    unsupported_trace = _replace_trace_step(
        trace,
        first_tool,
        first_tool.model_copy(update={"redacted_input": unsupported_payload}),
    )
    unsupported_result = evaluate_trajectory_case(
        case=case,
        trace=unsupported_trace,
        case_result=case_result,
        registry=registry,
    )

    shadow_claim = OutputClaim(
        claim_id="mutation:unsupported-inventory",
        category="inventory",
        value="SYNTHETIC-NONEXISTENT-REF",
        evidence_refs=("SYNTHETIC-NONEXISTENT-REF",),
    )
    shadow_output = case_result.user_output.model_copy(
        update={"claims": (*case_result.user_output.claims, shadow_claim)}
    )
    shadow_case_result = case_result.model_copy(update={"user_output": shadow_output})
    shadow_result = evaluate_trajectory_case(
        case=case,
        trace=trace,
        case_result=shadow_case_result,
        registry=registry,
    )

    outbound = next(
        step for step in tool_steps if step.name == "provider.search_transport.outbound"
    )
    inbound = next(
        step for step in tool_steps if step.name == "provider.search_transport.inbound"
    )
    order_trace = _replace_trace_step(
        trace,
        outbound,
        outbound.model_copy(update={"sequence": inbound.sequence + 1}),
    )
    order_result = evaluate_trajectory_case(
        case=case,
        trace=order_trace,
        case_result=case_result,
        registry=registry,
    )

    return (
        MutationCheck(
            mutation_id="unknown_tool_name",
            expected_detection="tool",
            detected=_has_finding(unknown_tool_result, "tool"),
        ),
        MutationCheck(
            mutation_id="missing_required_parameter",
            expected_detection="parameter_schema",
            detected=_has_finding(schema_result, "parameter_schema"),
        ),
        MutationCheck(
            mutation_id="unsupported_parameter_value",
            expected_detection="parameter_unsupported",
            detected=_has_finding(unsupported_result, "parameter_unsupported"),
        ),
        MutationCheck(
            mutation_id="claim_without_tool_evidence",
            expected_detection="shadow",
            detected=_has_finding(shadow_result, "shadow"),
        ),
        MutationCheck(
            mutation_id="outbound_after_inbound",
            expected_detection="trajectory_order",
            detected=any(not check.passed for check in order_result.order_checks),
        ),
    )


def expected_tool_path(
    case: WorkflowEvaluationCase,
    *,
    model_in_the_loop: bool = False,
) -> tuple[str, ...]:
    """根据用例推导期望工具调用路径。"""

    searches = (
        "provider.search_transport.outbound",
        "provider.search_transport.inbound",
        "provider.search_hotels",
    )
    prefix = ("llm.extract_trip_intent",) if model_in_the_loop else ()
    if case.fault.action == "SEARCH_FAILURE":
        return (*prefix, *searches[:1])
    if case.scenario in {"REQUIRES_APPROVAL", "NO_FEASIBLE_OPTION"}:
        return (*prefix, *searches)
    if case.scenario == "REVALIDATION_CHANGED" or case.fault.action == "REVALIDATION_FAILURE":
        return (*prefix, *searches, "provider.revalidate")
    return (*prefix, *searches, "provider.revalidate", "provider.create_deep_link")


def trace_has_model_in_the_loop(trace: EvaluationTrace) -> bool:
    """判断轨迹是否包含模型参与步骤。"""

    return trace.evaluation_mode != "deterministic_mock"


def _order_checks(
    expected_tools: tuple[str, ...], tool_steps: tuple[TraceStep, ...]
) -> tuple[TrajectoryCheck, ...]:
    checks: list[TrajectoryCheck] = []
    for before, after in zip(expected_tools, expected_tools[1:], strict=False):
        before_steps = tuple(step.sequence for step in tool_steps if step.name == before)
        after_steps = tuple(step.sequence for step in tool_steps if step.name == after)
        passed = bool(before_steps and after_steps and min(before_steps) < min(after_steps))
        checks.append(
            TrajectoryCheck(
                check_id=f"order:{before}->{after}",
                passed=passed,
                expected="before",
                actual={"before": before_steps, "after": after_steps},
                evidence=tuple(
                    [f"step:{value}" for value in before_steps]
                    + [f"step:{value}" for value in after_steps]
                ),
            )
        )
    return tuple(checks)


def _parameter_schema_reasons(step: TraceStep, contract: ToolContract) -> tuple[str, ...]:
    payload = step.redacted_input
    if not isinstance(payload, dict):
        return ("PARAMETERS_NOT_OBJECT",)
    reasons: list[str] = []
    expected = set(contract.required_parameters)
    actual = set(payload)
    if missing := sorted(expected - actual):
        reasons.append("MISSING:" + ",".join(missing))
    if extra := sorted(actual - expected):
        reasons.append("UNEXPECTED:" + ",".join(extra))
    for key, expected_type in contract.parameter_types.items():
        if key in payload and not _matches_parameter_type(payload[key], expected_type):
            reasons.append(f"WRONG_TYPE:{key}:{expected_type}")
    return tuple(reasons)


def _matches_parameter_type(value: Any, expected_type: ParameterType) -> bool:
    if expected_type == "string":
        return isinstance(value, str) and bool(value)
    if expected_type == "datetime":
        return isinstance(value, str) and _parse_datetime(value) is not None
    if expected_type == "date":
        return isinstance(value, str) and _parse_date(value) is not None
    if expected_type == "string_array":
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and bool(item) for item in value)
        )
    if expected_type == "positive_integer":
        return type(value) is int and value > 0
    if expected_type == "object":
        return isinstance(value, dict) and bool(value)
    return False


def _unsupported_parameter_reasons(
    *,
    step: TraceStep,
    case: WorkflowEvaluationCase,
    case_result: WorkflowCaseEvaluation,
    prior_steps: tuple[TraceStep, ...],
) -> tuple[str, ...]:
    payload = step.redacted_input or {}
    request = case.request
    expected: dict[str, Any]
    if step.name == "provider.search_transport.outbound":
        expected = {
            "origin": request.origin,
            "destination": request.destination,
            "depart_after": request.departure_after,
            "arrive_before": request.arrive_by,
        }
    elif step.name == "provider.search_transport.inbound":
        expected = {
            "origin": request.destination,
            "destination": request.origin,
            "depart_after": request.return_after,
            "arrive_before": request.return_before,
        }
    elif step.name == "provider.search_hotels":
        expected = {
            "city": request.destination,
            "check_in": request.hotel_check_in,
            "check_out": request.hotel_check_out,
        }
    elif step.name == "provider.revalidate":
        expected = {"refs": list(case_result.user_output.evidence_refs)}
    elif step.name == "provider.create_deep_link":
        expected = {
            "option_id": case_result.user_output.selected_option_id,
            "inventory_refs": list(case_result.user_output.evidence_refs),
        }
    else:
        return ()

    reasons: list[str] = []
    for key, expected_value in expected.items():
        actual = payload.get(key)
        if not _same_value(actual, expected_value):
            reasons.append(f"UNSUPPORTED_VALUE:{key}")
    if step.name in {"provider.revalidate", "provider.create_deep_link"}:
        prior_refs = {
            ref
            for prior in prior_steps
            if prior.status == "success"
            and prior.name
            in {
                "provider.search_transport.outbound",
                "provider.search_transport.inbound",
                "provider.search_hotels",
            }
            for ref in prior.evidence_refs
        }
        key = "refs" if step.name == "provider.revalidate" else "inventory_refs"
        actual_refs = set(payload.get(key, []))
        if not actual_refs.issubset(prior_refs):
            reasons.append(f"NO_PRIOR_EVIDENCE:{key}")
    return tuple(reasons)


def _shadow_reason(claim: Any, trace: EvaluationTrace) -> str | None:
    successful_search_refs = {
        ref
        for step in trace.steps
        if step.kind == "tool"
        and step.status == "success"
        and step.name
        in {
            "provider.search_transport.outbound",
            "provider.search_transport.inbound",
            "provider.search_hotels",
        }
        for ref in step.evidence_refs
    }
    if not claim.evidence_refs:
        return "CLAIM_HAS_NO_EVIDENCE_REFERENCE"
    if claim.category == "inventory":
        if claim.value not in successful_search_refs:
            return "INVENTORY_VALUE_NOT_IN_TOOL_EVIDENCE"
        if claim.value not in claim.evidence_refs:
            return "INVENTORY_VALUE_NOT_CITED"
        return None
    if claim.category == "policy":
        policy_refs = {
            ref
            for step in trace.steps
            if step.kind == "policy" and step.status == "success"
            for ref in step.evidence_refs
        }
        if not set(claim.evidence_refs).issubset(policy_refs):
            return "POLICY_CLAIM_NOT_GROUNDED_IN_POLICY_STEP"
        if claim.value != trace.final.policy_outcome:
            return "POLICY_CLAIM_DISAGREES_WITH_FINAL_STATE"
        return None
    if claim.category == "workflow":
        deep_link_refs = {
            ref
            for step in trace.steps
            if step.kind == "tool"
            and step.name == "provider.create_deep_link"
            and step.status == "success"
            for ref in step.evidence_refs
        }
        if claim.value != "READY_FOR_HANDOFF" or trace.final.state != claim.value:
            return "WORKFLOW_CLAIM_DISAGREES_WITH_FINAL_STATE"
        if not set(claim.evidence_refs).issubset(deep_link_refs):
            return "WORKFLOW_CLAIM_HAS_NO_SUCCESSFUL_HANDOFF_TOOL"
        return None
    return "UNSUPPORTED_CLAIM_CATEGORY"


def _same_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, datetime):
        return _parse_datetime(actual) == expected
    if isinstance(expected, date):
        return _parse_date(actual) == expected
    return actual == expected


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _affected_runs(
    evaluations: tuple[TrajectoryCaseEvaluation, ...],
    finding_types: set[HallucinationType],
) -> int:
    return sum(
        any(finding.finding_type in finding_types for finding in item.findings)
        for item in evaluations
    )


def _replace_trace_step(
    trace: EvaluationTrace,
    original: TraceStep,
    replacement: TraceStep,
) -> EvaluationTrace:
    return trace.model_copy(
        update={
            "steps": tuple(
                replacement if step is original else step for step in trace.steps
            )
        }
    )


def _has_finding(
    result: TrajectoryCaseEvaluation,
    finding_type: HallucinationType,
) -> bool:
    return any(item.finding_type == finding_type for item in result.findings)


def _rate(
    numerator: int,
    denominator: int,
    *,
    exposure_note: str | None = None,
) -> MetricResult:
    if denominator == 0:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=float(numerator),
            denominator=0.0,
            unit="rate",
            exposure_note=exposure_note,
            confidence_note="No applicable denominator; value is not coerced to zero.",
        )
    return MetricResult(
        status="measured",
        value=numerator / denominator,
        numerator=float(numerator),
        denominator=float(denominator),
        unit="rate",
        exposure_note=exposure_note,
        confidence_note="Deterministic D1 offline trajectory evaluation.",
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
