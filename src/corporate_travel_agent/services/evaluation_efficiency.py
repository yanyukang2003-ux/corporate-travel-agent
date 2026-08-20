"""评测什么：工具调用效率（冗余、路径长度、相对期望路径的浪费）。"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

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
    WorkflowCaseEvaluation,
)
from corporate_travel_agent.services.evaluation_trace import EvaluationTrace, TraceStep
from corporate_travel_agent.services.evaluation_trajectory import (
    ToolRegistry,
    TrajectoryCaseEvaluation,
    evaluate_trajectory_case,
    expected_tool_path,
    load_tool_registry,
    trace_has_model_in_the_loop,
)

EFFICIENCY_EVALUATOR_VERSION = "tool-efficiency-evaluator-v1"
TOOL_CALL_BUDGET = 12
SHA256_PATTERN = r"^[a-f0-9]{64}$"

MutationDetection = Literal[
    "invalid_call",
    "duplicate_call",
    "redundant_call",
    "no_gain_call",
    "minimal_path_mismatch",
    "budget_exceeded",
]


class EfficiencyEvaluationError(RuntimeError):
    """效率评测失败。"""
    pass


class EfficiencyModel(BaseModel):
    """效率评测 Pydantic 模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolCallEfficiency(EfficiencyModel):
    """单用例工具调用效率指标。"""
    call_index: int = Field(ge=1)
    step_sequence: int = Field(ge=1)
    tool_name: str
    status: str
    invalid: bool
    invalid_reasons: tuple[str, ...]
    duplicate: bool
    duplicate_of_sequence: int | None = Field(default=None, ge=1)
    no_gain: bool
    useful_signals: tuple[str, ...]
    redundant: bool
    redundancy_reasons: tuple[str, ...]


class ToolEfficiencyCaseEvaluation(EfficiencyModel):
    """单用例效率评测结果。"""
    run_id: str
    case_id: str
    scenario: str
    normal_run: bool
    task_passed: bool
    tool_calls: int = Field(ge=0)
    expected_minimum_calls: int = Field(ge=0)
    excess_calls: int = Field(ge=0)
    minimal_path_match: bool
    budget_limit: Literal[12] = TOOL_CALL_BUDGET
    budget_compliant: bool
    calls: tuple[ToolCallEfficiency, ...]
    efficiency_pass: bool


class EfficiencyMutationCheck(EfficiencyModel):
    """效率评测器突变检查结果。"""
    mutation_id: str
    expected_detection: MutationDetection
    detected: bool


class EfficiencyRunSummary(EfficiencyModel):
    """效率评测运行汇总。"""
    schema_version: Literal[1] = 1
    evaluation_id: str
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    evaluator_version: Literal["tool-efficiency-evaluator-v1"] = (
        EFFICIENCY_EVALUATOR_VERSION
    )
    generated_at: datetime
    evaluation_mode: Literal["deterministic_mock_offline_replay"]
    dataset_id: str
    dataset_version: str
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    selected_cases: int = Field(ge=1)
    completed_runs: int = Field(ge=0)
    source_trace_file: str
    source_trace_sha256: str = Field(pattern=SHA256_PATTERN)
    source_case_results_file: str
    source_case_results_sha256: str = Field(pattern=SHA256_PATTERN)
    source_trajectory_results_file: str
    source_trajectory_results_sha256: str = Field(pattern=SHA256_PATTERN)
    tool_registry_file: str
    tool_registry_sha256: str = Field(pattern=SHA256_PATTERN)
    efficiency_case_results_file: str
    efficiency_case_results_sha256: str = Field(pattern=SHA256_PATTERN)
    evaluation_result_file: str
    evaluation_result_sha256: str = Field(pattern=SHA256_PATTERN)
    total_tool_calls: int = Field(ge=0)
    successful_tasks: int = Field(ge=0)
    stage_4_gate_passed: bool
    full_protocol_gate: Literal["not_evaluated"] = "not_evaluated"
    percentile_method: Literal["linear_type_7"] = "linear_type_7"
    mutation_checks: tuple[EfficiencyMutationCheck, ...]
    limitations: tuple[str, ...]


def evaluate_tool_efficiency_case(
    *,
    case: WorkflowEvaluationCase,
    trace: EvaluationTrace,
    task_result: WorkflowCaseEvaluation,
    trajectory_result: TrajectoryCaseEvaluation,
) -> ToolEfficiencyCaseEvaluation:
    """评估单条轨迹的工具调用效率。"""
    if not (
        case.case_id == trace.case_id == task_result.case_id == trajectory_result.case_id
    ):
        raise EfficiencyEvaluationError("case IDs do not match")
    if not (
        trace.run_id == task_result.run_id == trajectory_result.run_id
    ):
        raise EfficiencyEvaluationError("run IDs do not match")

    expected_path = expected_tool_path(
        case, model_in_the_loop=trace_has_model_in_the_loop(trace)
    )
    expected_counts = Counter(expected_path)
    invalid_reasons = _invalid_reasons_by_sequence(trajectory_result)
    full_steps = trace.steps
    tool_positions = tuple(
        (position, step)
        for position, step in enumerate(full_steps)
        if step.kind == "tool"
    )
    prior_signature_positions: dict[tuple[str | None, str | None, str | None], list[int]] = (
        defaultdict(list)
    )
    seen_evidence: set[str] = set()
    seen_name_counts: Counter[str] = Counter()
    calls: list[ToolCallEfficiency] = []

    for call_index, (position, step) in enumerate(tool_positions, start=1):
        signature = (step.name, step.input_hash, step.state_before)
        duplicate_of: int | None = None
        for prior_position in reversed(prior_signature_positions[signature]):
            if not _has_new_information(full_steps[prior_position + 1 : position]):
                duplicate_of = full_steps[prior_position].sequence
                break
        duplicate = duplicate_of is not None
        prior_signature_positions[signature].append(position)

        new_refs = tuple(ref for ref in step.evidence_refs if ref not in seen_evidence)
        useful_signals: list[str] = []
        if new_refs:
            useful_signals.append(f"new_evidence:{len(new_refs)}")
        if step.status in {"failure", "partial"} and (
            step.error_type or step.reason_code or step.status == "failure"
        ):
            useful_signals.append("actionable_error")
        if step.state_before != step.state_after:
            useful_signals.append("state_change")
        if not duplicate and step.status == "success":
            # Intent extraction produces the structured request the rest of the
            # run depends on. It changes no state and cites no inventory, so it
            # needs its own signal or it reads as a no-gain call.
            if step.name == "llm.extract_trip_intent" and step.name in expected_path:
                useful_signals.append("intent_structured")
            if step.name == "provider.revalidate" and step.name in expected_path:
                useful_signals.append("mandatory_safety_revalidation")
            if step.name == "provider.create_deep_link" and step.name in expected_path:
                useful_signals.append("handoff_artifact_created")
            if (
                step.name
                and step.name.startswith("provider.search_")
                and not step.evidence_refs
                and trace.final.state == "NO_FEASIBLE_OPTION"
            ):
                useful_signals.append("actionable_empty_result")
        justified_retry = step.retry_of is not None and bool(step.reason_code)
        if justified_retry:
            useful_signals.append("justified_retry")

        if step.status == "success":
            seen_evidence.update(step.evidence_refs)
        seen_name_counts[step.name or ""] += 1
        no_gain = not useful_signals
        call_invalid_reasons = invalid_reasons.get(step.sequence, ())
        redundancy_reasons: list[str] = []
        if call_invalid_reasons:
            redundancy_reasons.append("INVALID_CALL")
        # Protocol 6.4: a retry carrying a reason code is recovery work, not waste.
        # It repeats an earlier call by design, so it is exempt from the duplicate
        # and minimal-path counters but still consumes budget and is reported.
        if duplicate and not justified_retry:
            redundancy_reasons.append("DUPLICATE_WITHOUT_NEW_INFORMATION")
        if no_gain:
            redundancy_reasons.append("NO_INFORMATION_GAIN")
        if step.name not in expected_counts:
            redundancy_reasons.append("OUTSIDE_MINIMAL_SCENARIO_PATH")
        elif seen_name_counts[step.name] > expected_counts[step.name] and not justified_retry:
            redundancy_reasons.append("EXCEEDS_MINIMAL_PATH_COUNT")
        calls.append(
            ToolCallEfficiency(
                call_index=call_index,
                step_sequence=step.sequence,
                tool_name=step.name or "[MISSING_TOOL_NAME]",
                status=step.status,
                invalid=bool(call_invalid_reasons),
                invalid_reasons=call_invalid_reasons,
                duplicate=duplicate,
                duplicate_of_sequence=duplicate_of,
                no_gain=no_gain,
                useful_signals=tuple(useful_signals),
                redundant=bool(redundancy_reasons),
                redundancy_reasons=tuple(redundancy_reasons),
            )
        )

    names = tuple(step.name for _, step in tool_positions)
    minimal_path_match = names == expected_path
    tool_call_count = len(tool_positions)
    budget_compliant = tool_call_count <= TOOL_CALL_BUDGET
    efficiency_pass = (
        minimal_path_match
        and budget_compliant
        and not any(
            item.invalid or item.duplicate or item.no_gain or item.redundant
            for item in calls
        )
    )
    return ToolEfficiencyCaseEvaluation(
        run_id=trace.run_id,
        case_id=case.case_id,
        scenario=case.scenario,
        normal_run=case.fault.action == "NONE",
        task_passed=task_result.hard_pass,
        tool_calls=tool_call_count,
        expected_minimum_calls=len(expected_path),
        excess_calls=max(0, tool_call_count - len(expected_path)),
        minimal_path_match=minimal_path_match,
        budget_compliant=budget_compliant,
        calls=tuple(calls),
        efficiency_pass=efficiency_pass,
    )


def evaluate_tool_efficiency_run(
    *,
    dataset_directory: str | Path,
    source_run_directory: str | Path,
    trajectory_run_directory: str | Path,
    output_directory: str | Path,
    tool_registry_path: str | Path,
) -> EfficiencyRunSummary:
    """对整次运行批量评估工具效率。"""
    dataset = load_evaluation_dataset(dataset_directory)
    source_root = Path(source_run_directory).expanduser().resolve()
    trajectory_root = Path(trajectory_run_directory).expanduser().resolve()
    output_root = Path(output_directory).expanduser().resolve()
    registry_path = Path(tool_registry_path).expanduser().resolve()
    if output_root.exists():
        raise EfficiencyEvaluationError(f"Output directory already exists: {output_root}")

    trace_path = source_root / "traces.jsonl"
    task_result_path = source_root / "case-results.jsonl"
    trajectory_result_path = trajectory_root / "trajectory-case-results.jsonl"
    trace_payload = trace_path.read_bytes()
    task_payload = task_result_path.read_bytes()
    trajectory_payload = trajectory_result_path.read_bytes()
    registry_payload = registry_path.read_bytes()
    traces = _parse_jsonl(trace_payload, EvaluationTrace)
    task_results = _parse_jsonl(task_payload, WorkflowCaseEvaluation)
    trajectory_results = _parse_jsonl(trajectory_payload, TrajectoryCaseEvaluation)
    registry = load_tool_registry(registry_path)
    cases = {case.case_id: case for case in dataset.workflow_cases}
    tasks_by_run = {item.run_id: item for item in task_results}
    trajectories_by_run = {item.run_id: item for item in trajectory_results}
    if not traces:
        raise EfficiencyEvaluationError("source run has no traces")
    if not (
        len(traces) == len(tasks_by_run) == len(trajectories_by_run)
    ):
        raise EfficiencyEvaluationError("source artifact coverage does not match")
    if any(trace.run_id not in tasks_by_run for trace in traces) or any(
        trace.run_id not in trajectories_by_run for trace in traces
    ):
        raise EfficiencyEvaluationError("source artifacts do not contain matching run IDs")
    if any(trace.case_id not in cases for trace in traces):
        raise EfficiencyEvaluationError("source run includes a case outside frozen D1")
    if any(
        trace.fingerprint.dataset_sha256 != dataset.manifest.workflow_cases.sha256
        for trace in traces
    ):
        raise EfficiencyEvaluationError("source trace dataset fingerprint does not match D1")

    evaluations = tuple(
        evaluate_tool_efficiency_case(
            case=cases[trace.case_id],
            trace=trace,
            task_result=tasks_by_run[trace.run_id],
            trajectory_result=trajectories_by_run[trace.run_id],
        )
        for trace in traces
    )
    evaluation_id = f"eval-{uuid4()}"
    formal_result = build_efficiency_evaluation_result(
        evaluation_id=evaluation_id,
        dataset_id=traces[0].fingerprint.dataset_id,
        dataset_version=dataset.manifest.dataset_version,
        evaluations=evaluations,
    )
    output_root.mkdir(parents=True)
    cases_output = output_root / "tool-efficiency-case-results.jsonl"
    cases_payload = "".join(
        f"{item.model_dump_json()}\n" for item in evaluations
    ).encode("utf-8")
    cases_output.write_bytes(cases_payload)
    result_output = output_root / "tool-efficiency-evaluation-result.json"
    result_payload = formal_result.model_dump_json(indent=2).encode("utf-8")
    result_output.write_bytes(result_payload)

    mutation_trace = next(
        (
            trace
            for trace in traces
            if cases[trace.case_id].scenario == "COMPLIANT"
        ),
        None,
    )
    summary = EfficiencyRunSummary(
        evaluation_id=evaluation_id,
        generated_at=datetime.now(UTC),
        evaluation_mode="deterministic_mock_offline_replay",
        dataset_id=traces[0].fingerprint.dataset_id,
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.workflow_cases.sha256,
        selected_cases=len({item.case_id for item in evaluations}),
        completed_runs=len(evaluations),
        source_trace_file=str(trace_path),
        source_trace_sha256=_sha256(trace_payload),
        source_case_results_file=str(task_result_path),
        source_case_results_sha256=_sha256(task_payload),
        source_trajectory_results_file=str(trajectory_result_path),
        source_trajectory_results_sha256=_sha256(trajectory_payload),
        tool_registry_file=str(registry_path),
        tool_registry_sha256=_sha256(registry_payload),
        efficiency_case_results_file=cases_output.name,
        efficiency_case_results_sha256=_sha256(cases_payload),
        evaluation_result_file=result_output.name,
        evaluation_result_sha256=_sha256(result_payload),
        total_tool_calls=sum(item.tool_calls for item in evaluations),
        successful_tasks=sum(item.task_passed for item in evaluations),
        stage_4_gate_passed=bool(formal_result.metrics["stage_4_rule_gate"].value),
        mutation_checks=(
            run_efficiency_mutation_checks(
                case=cases[mutation_trace.case_id],
                trace=mutation_trace,
                task_result=tasks_by_run[mutation_trace.run_id],
                registry=registry,
            )
            if mutation_trace is not None
            else ()
        ),
        limitations=(
            "All calls are from deterministic MOCK workflow traces.",
            "Redundancy is a rule-based minimal-path counterfactual, not a live "
            "tool-ablation experiment.",
            "Information gain uses new evidence, state changes, mandatory revalidation, "
            "handoff creation, and actionable errors as conservative proxies.",
            "Provider result payloads are hashed rather than stored, so semantic "
            "information gain beyond recorded evidence cannot be reconstructed.",
            "Real-model retries, stochastic planning, token cost, and network latency "
            "are not measured in this stage.",
        ),
    )
    (output_root / "tool-efficiency-run-summary.json").write_text(
        summary.model_dump_json(indent=2), encoding="utf-8"
    )
    return summary


def build_efficiency_evaluation_result(
    *,
    evaluation_id: str,
    dataset_id: str,
    dataset_version: str,
    evaluations: tuple[ToolEfficiencyCaseEvaluation, ...],
) -> EvaluationResult:
    """汇总效率评测结果与门禁指标。"""
    runs = len(evaluations)
    calls = tuple(call for item in evaluations for call in item.calls)
    total_calls = len(calls)
    normal_calls = tuple(
        call for item in evaluations if item.normal_run for call in item.calls
    )
    successful_counts = sorted(
        item.tool_calls for item in evaluations if item.task_passed
    )
    invalid_count = sum(item.invalid for item in calls)
    duplicate_count = sum(item.duplicate for item in calls)
    normal_duplicate_count = sum(item.duplicate for item in normal_calls)
    redundant_count = sum(item.redundant for item in calls)
    no_gain_count = sum(item.no_gain for item in calls)
    excess_count = sum(item.excess_calls for item in evaluations)
    path_matches = sum(item.minimal_path_match for item in evaluations)
    budget_compliant = sum(item.budget_compliant for item in evaluations)
    efficiency_passes = sum(item.efficiency_pass for item in evaluations)
    p95 = _percentile(successful_counts, 0.95)
    max_calls = float(max(successful_counts)) if successful_counts else None
    redundant_rate = redundant_count / total_calls if total_calls else None
    gate_pass = (
        invalid_count == 0
        and normal_duplicate_count == 0
        and no_gain_count == 0
        and redundant_rate is not None
        and redundant_rate <= 0.10
        and path_matches == runs
        and p95 is not None
        and p95 <= 7
        and max_calls is not None
        and max_calls <= TOOL_CALL_BUDGET
    )
    metrics = {
        "efficiency_pass_rate": _rate(efficiency_passes, runs),
        "invalid_call_rate": _rate(invalid_count, total_calls),
        "duplicate_call_rate": _rate(duplicate_count, total_calls),
        "normal_duplicate_call_rate": _rate(
            normal_duplicate_count, len(normal_calls)
        ),
        "redundant_call_rate": _rate(redundant_count, total_calls),
        "no_gain_call_rate": _rate(no_gain_count, total_calls),
        "excess_call_rate": _rate(excess_count, total_calls),
        "minimal_path_match_rate": _rate(path_matches, runs),
        "tool_budget_compliance_rate": _rate(budget_compliant, runs),
        "tool_calls_per_passed_task_mean": _mean_metric(successful_counts),
        "tool_calls_per_passed_task_p50": _percentile_metric(successful_counts, 0.50),
        "tool_calls_per_passed_task_p95": _percentile_metric(successful_counts, 0.95),
        "tool_calls_per_passed_task_max": _max_metric(successful_counts),
        "stage_4_rule_gate": MetricResult(
            status="measured",
            value=float(gate_pass),
            numerator=float(gate_pass),
            denominator=1.0,
            unit="boolean",
            confidence_note="Stage-only rule gate; full protocol gate remains not evaluated.",
        ),
    }
    slices: dict[str, Any] = {}
    for scenario in sorted({item.scenario for item in evaluations}):
        items = tuple(item for item in evaluations if item.scenario == scenario)
        scenario_calls = tuple(call for item in items for call in item.calls)
        slices[scenario] = {
            "runs": len(items),
            "tool_calls": len(scenario_calls),
            "efficiency_passed": sum(item.efficiency_pass for item in items),
            "minimum_calls": sum(item.expected_minimum_calls for item in items),
            "excess_calls": sum(item.excess_calls for item in items),
        }
        metrics[f"efficiency_pass_rate.scenario.{scenario}"] = _rate(
            sum(item.efficiency_pass for item in items), len(items)
        )
        metrics[f"tool_calls_mean.scenario.{scenario}"] = _mean_metric(
            [item.tool_calls for item in items]
        )

    hard_failures: list[HardFailure] = []
    bad_cases: list[BadCaseCandidate] = []
    for item in evaluations:
        if item.efficiency_pass:
            continue
        reasons = tuple(
            sorted(
                {
                    reason
                    for call in item.calls
                    for reason in (
                        *call.invalid_reasons,
                        *call.redundancy_reasons,
                    )
                }
                | ({"MINIMAL_PATH_MISMATCH"} if not item.minimal_path_match else set())
                | ({"TOOL_BUDGET_EXCEEDED"} if not item.budget_compliant else set())
            )
        )
        signature = ",".join(reasons)
        hard_failures.append(
            HardFailure(
                case_id=item.case_id,
                run_id=item.run_id,
                failure_type=signature,
                evidence=tuple(
                    f"step:{call.step_sequence}"
                    for call in item.calls
                    if call.invalid or call.duplicate or call.no_gain or call.redundant
                ),
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


def run_efficiency_mutation_checks(
    *,
    case: WorkflowEvaluationCase,
    trace: EvaluationTrace,
    task_result: WorkflowCaseEvaluation,
    registry: ToolRegistry,
) -> tuple[EfficiencyMutationCheck, ...]:
    """运行效率评测器自身的突变检查。"""
    first_tool = next(step for step in trace.steps if step.kind == "tool")
    invalid_trace = _replace_step(
        trace,
        first_tool,
        first_tool.model_copy(update={"state_before": "DRAFT"}),
    )
    invalid_trajectory = evaluate_trajectory_case(
        case=case,
        trace=invalid_trace,
        case_result=task_result,
        registry=registry,
    )
    invalid_result = evaluate_tool_efficiency_case(
        case=case,
        trace=invalid_trace,
        task_result=task_result,
        trajectory_result=invalid_trajectory,
    )

    duplicate_trace = _insert_copies(trace, first_tool, 1)
    duplicate_trajectory = evaluate_trajectory_case(
        case=case,
        trace=duplicate_trace,
        case_result=task_result,
        registry=registry,
    )
    duplicate_result = evaluate_tool_efficiency_case(
        case=case,
        trace=duplicate_trace,
        task_result=task_result,
        trajectory_result=duplicate_trajectory,
    )

    budget_trace = _insert_copies(trace, first_tool, 8)
    budget_trajectory = evaluate_trajectory_case(
        case=case,
        trace=budget_trace,
        case_result=task_result,
        registry=registry,
    )
    budget_result = evaluate_tool_efficiency_case(
        case=case,
        trace=budget_trace,
        task_result=task_result,
        trajectory_result=budget_trajectory,
    )
    return (
        EfficiencyMutationCheck(
            mutation_id="tool_in_disallowed_state",
            expected_detection="invalid_call",
            detected=any(call.invalid for call in invalid_result.calls),
        ),
        EfficiencyMutationCheck(
            mutation_id="adjacent_exact_duplicate",
            expected_detection="duplicate_call",
            detected=any(call.duplicate for call in duplicate_result.calls),
        ),
        EfficiencyMutationCheck(
            mutation_id="duplicate_is_removable",
            expected_detection="redundant_call",
            detected=any(call.redundant for call in duplicate_result.calls),
        ),
        EfficiencyMutationCheck(
            mutation_id="duplicate_adds_no_evidence",
            expected_detection="no_gain_call",
            detected=any(call.no_gain for call in duplicate_result.calls),
        ),
        EfficiencyMutationCheck(
            mutation_id="extra_call_breaks_minimal_path",
            expected_detection="minimal_path_mismatch",
            detected=not duplicate_result.minimal_path_match,
        ),
        EfficiencyMutationCheck(
            mutation_id="thirteen_tool_calls",
            expected_detection="budget_exceeded",
            detected=not budget_result.budget_compliant,
        ),
    )


def _invalid_reasons_by_sequence(
    trajectory_result: TrajectoryCaseEvaluation,
) -> dict[int, tuple[str, ...]]:
    values: dict[int, list[str]] = defaultdict(list)
    for finding in trajectory_result.findings:
        if finding.finding_type in {
            "tool",
            "parameter_schema",
            "parameter_unsupported",
        } and finding.step_sequence is not None:
            values[finding.step_sequence].append(finding.reason_code)
    for check in trajectory_result.state_checks:
        if check.passed:
            continue
        for value in check.evidence:
            if value.startswith("step:"):
                values[int(value.removeprefix("step:"))].append("DISALLOWED_STATE")
    return {key: tuple(dict.fromkeys(items)) for key, items in values.items()}


def _has_new_information(steps: tuple[TraceStep, ...]) -> bool:
    return any(
        step.evidence_refs
        or step.state_before != step.state_after
        or step.status in {"failure", "partial"}
        or step.kind in {"user", "policy", "recovery"}
        for step in steps
    )


def _parse_jsonl(payload: bytes, model: type[BaseModel]) -> tuple[Any, ...]:
    return tuple(
        model.model_validate_json(line)
        for line in payload.decode("utf-8").splitlines()
        if line
    )


def _insert_copies(
    trace: EvaluationTrace,
    target: TraceStep,
    copies: int,
) -> EvaluationTrace:
    expanded: list[TraceStep] = []
    for step in trace.steps:
        expanded.append(step)
        if step is target:
            expanded.extend(target.model_copy(deep=True) for _ in range(copies))
    renumbered = tuple(
        step.model_copy(update={"sequence": index})
        for index, step in enumerate(expanded, start=1)
    )
    return trace.model_copy(update={"steps": renumbered})


def _replace_step(
    trace: EvaluationTrace,
    target: TraceStep,
    replacement: TraceStep,
) -> EvaluationTrace:
    return trace.model_copy(
        update={
            "steps": tuple(
                replacement if step is target else step for step in trace.steps
            )
        }
    )


def _rate(numerator: int, denominator: int) -> MetricResult:
    if denominator == 0:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=float(numerator),
            denominator=0.0,
            unit="rate",
            confidence_note="No applicable denominator; value is not coerced to zero.",
        )
    return MetricResult(
        status="measured",
        value=numerator / denominator,
        numerator=float(numerator),
        denominator=float(denominator),
        unit="rate",
        confidence_note="Deterministic D1 offline tool-efficiency evaluation.",
    )


def _mean_metric(values: list[int]) -> MetricResult:
    if not values:
        return _unavailable_stat("calls")
    return MetricResult(
        status="measured",
        value=sum(values) / len(values),
        numerator=float(sum(values)),
        denominator=float(len(values)),
        unit="calls",
        confidence_note="Only stage-2 hard-passed tasks are included.",
    )


def _percentile_metric(values: list[int], quantile: float) -> MetricResult:
    value = _percentile(values, quantile)
    if value is None:
        return _unavailable_stat("calls")
    return MetricResult(
        status="measured",
        value=value,
        numerator=None,
        denominator=float(len(values)),
        unit="calls",
        confidence_note="Linear type-7 percentile over stage-2 hard-passed tasks.",
    )


def _max_metric(values: list[int]) -> MetricResult:
    if not values:
        return _unavailable_stat("calls")
    return MetricResult(
        status="measured",
        value=float(max(values)),
        numerator=None,
        denominator=float(len(values)),
        unit="calls",
        confidence_note="Maximum over stage-2 hard-passed tasks.",
    )


def _unavailable_stat(unit: str) -> MetricResult:
    return MetricResult(
        status="unavailable",
        value=None,
        numerator=None,
        denominator=0.0,
        unit=unit,
        confidence_note="No successful task is available for this statistic.",
    )


def _percentile(values: list[int], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
