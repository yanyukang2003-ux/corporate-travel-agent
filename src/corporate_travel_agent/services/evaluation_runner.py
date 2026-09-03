"""评测什么：确定性/模型工作流评测运行器——编排数据集执行、追踪与质量汇总。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent import __version__
from corporate_travel_agent.services.evaluation_dataset import (
    LoadedEvaluationDataset,
    WorkflowEvaluationCase,
    WorkflowEvaluationObservation,
    load_evaluation_dataset,
    run_workflow_evaluation_case,
)
from corporate_travel_agent.services.evaluation_quality import (
    EvaluationResult,
    JudgeInput,
    WorkflowCaseEvaluation,
    build_evaluation_result,
    build_judge_input,
    evaluate_workflow_case,
)
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationMode,
    EvaluationTrace,
    EvaluationTraceRecorder,
    TraceFinal,
    TraceFingerprint,
)

RUNNER_VERSION = "agent-eval-runner-v1"


class EvaluationRunnerError(RuntimeError):
    """评测运行器失败。"""
    pass


class RunnerModel(BaseModel):
    """运行器结果模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class CaseRunSummary(RunnerModel):
    """单用例运行摘要。"""
    run_id: str
    case_id: str
    attempt: int = Field(ge=1)
    passed: bool
    mismatches: tuple[str, ...]
    trace_steps: int = Field(ge=1)
    tool_steps: int = Field(ge=0)
    rule_quality_passed: bool | None = None
    rule_quality_score: float | None = Field(default=None, ge=0, le=1)


class EvaluationRunSummary(RunnerModel):
    """整次评测运行摘要。"""
    schema_version: int = 1
    evaluation_id: str
    protocol_id: str = "agent-eval-v1"
    runner_version: str = RUNNER_VERSION
    generated_at: datetime
    evaluation_mode: str
    dataset_id: str
    dataset_version: str
    dataset_sha256: str
    selected_cases: int = Field(ge=1)
    attempts_per_case: int = Field(ge=1)
    expected_runs: int = Field(ge=1)
    completed_runs: int = Field(ge=0)
    passed_runs: int = Field(ge=0)
    trace_validation_passed: bool
    traces_file: str
    traces_sha256: str
    case_results_file: str | None = None
    case_results_sha256: str | None = None
    evaluation_result_file: str | None = None
    evaluation_result_sha256: str | None = None
    judge_inputs_file: str | None = None
    judge_inputs_sha256: str | None = None
    stage_2_rule_gate_passed: bool | None = None
    real_model_calls: int = Field(ge=0)
    estimated_cost: float | None
    limitations: tuple[str, ...]
    runs: tuple[CaseRunSummary, ...]


def run_deterministic_workflow_evaluation(
    dataset_directory: str | Path,
    output_directory: str | Path,
    *,
    attempts_per_case: int = 1,
    case_ids: tuple[str, ...] | None = None,
    code_revision: str | None = None,
) -> EvaluationRunSummary:
    """以确定性 mock 路径运行工作流评测。"""

    return _run_workflow_evaluation(
        dataset_directory,
        output_directory,
        attempts_per_case=attempts_per_case,
        case_ids=case_ids,
        code_revision=code_revision,
        language_model=None,
        evaluation_mode="deterministic_mock",
        price_table_version=None,
    )


def run_model_workflow_evaluation(
    dataset_directory: str | Path,
    output_directory: str | Path,
    *,
    language_model: object,
    attempts_per_case: int = 1,
    case_ids: tuple[str, ...] | None = None,
    code_revision: str | None = None,
    price_table_version: str | None = None,
) -> EvaluationRunSummary:
    """以模型参与路径运行工作流评测。"""

    if language_model is None:
        raise EvaluationRunnerError("a language model is required for a model run")
    return _run_workflow_evaluation(
        dataset_directory,
        output_directory,
        attempts_per_case=attempts_per_case,
        case_ids=case_ids,
        code_revision=code_revision,
        language_model=language_model,
        evaluation_mode="model_mock",
        price_table_version=price_table_version,
    )


def _run_workflow_evaluation(
    dataset_directory: str | Path,
    output_directory: str | Path,
    *,
    attempts_per_case: int,
    case_ids: tuple[str, ...] | None,
    code_revision: str | None,
    language_model: object | None,
    evaluation_mode: str,
    price_table_version: str | None,
) -> EvaluationRunSummary:
    if attempts_per_case < 1:
        raise EvaluationRunnerError("attempts_per_case must be at least 1")
    dataset = load_evaluation_dataset(dataset_directory)
    cases = _select_cases(dataset, case_ids)
    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise EvaluationRunnerError(f"Output directory already exists: {output_root}")
    output_root.mkdir(parents=True)

    fingerprint = TraceFingerprint(
        project_version=__version__,
        code_revision=code_revision,
        dataset_id=f"{dataset.manifest.dataset_id}-workflow",
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.workflow_cases.sha256,
        prompt_version=getattr(language_model, "prompt_version", None),
        requested_model=getattr(language_model, "model", None),
        actual_model=getattr(language_model, "model", None),
        runner_version=RUNNER_VERSION,
        price_table_version=price_table_version,
    )
    traces: list[EvaluationTrace] = []
    case_evaluations: list[WorkflowCaseEvaluation] = []
    run_summaries: list[CaseRunSummary] = []
    evaluation_id = f"eval-{uuid4()}"
    for case in cases:
        for attempt in range(1, attempts_per_case + 1):
            run_id = f"run-{uuid4()}"
            recorder = EvaluationTraceRecorder(
                run_id=run_id,
                case_id=case.case_id,
                attempt=attempt,
                evaluation_mode=cast(EvaluationMode, evaluation_mode),
                fingerprint=fingerprint,
                # 结构化请求：编排器决定每一步；产品入口：模型从固定工具表里挑。
                tool_choice_exposure=(
                    "orchestrator_controlled"
                    if language_model is None
                    else "allowlisted_model_choice"
                ),
            )
            # 有模型就走产品入口（工具循环）；没有就用冻结的结构化请求。
            observation = run_workflow_evaluation_case(
                case,
                trace_observer=recorder,
                tool_calling_language_model=language_model,
            )
            case_evaluation = evaluate_workflow_case(
                run_id=run_id,
                case=case,
                observation=observation,
            )
            mismatches = tuple(
                item.assertion_id
                for item in case_evaluation.hard_assertions
                if item.applicable and not item.passed
            )
            trace = recorder.finish(
                TraceFinal(
                    state=observation.final_state.value,
                    policy_outcome=(
                        observation.selected_policy_outcome.value
                        if observation.selected_policy_outcome is not None
                        else None
                    ),
                    booking_allowed=observation.booking_intent_created,
                    result_refs=observation.selected_inventory_refs,
                    user_response_hash=case_evaluation.user_output.output_hash,
                    failure_reason=observation.failure_reason,
                )
            )
            _validate_trace_invariants(trace, observation)
            traces.append(trace)
            case_evaluations.append(case_evaluation)
            run_summaries.append(
                CaseRunSummary(
                    run_id=run_id,
                    case_id=case.case_id,
                    attempt=attempt,
                    passed=not mismatches,
                    mismatches=mismatches,
                    trace_steps=len(trace.steps),
                    tool_steps=sum(step.kind == "tool" for step in trace.steps),
                    rule_quality_passed=case_evaluation.rule_quality_pass,
                    rule_quality_score=case_evaluation.rule_quality_score,
                )
            )

    trace_path = output_root / "traces.jsonl"
    trace_payload = "".join(
        f"{trace.model_dump_json()}\n" for trace in traces
    ).encode("utf-8")
    trace_path.write_bytes(trace_payload)
    case_results_path = output_root / "case-results.jsonl"
    case_results_payload = "".join(
        f"{item.model_dump_json()}\n" for item in case_evaluations
    ).encode("utf-8")
    case_results_path.write_bytes(case_results_payload)
    evaluation_result: EvaluationResult = build_evaluation_result(
        evaluation_id=evaluation_id,
        dataset_id=f"{dataset.manifest.dataset_id}-workflow",
        dataset_version=dataset.manifest.dataset_version,
        selected_cases=len(cases),
        expected_runs=len(cases) * attempts_per_case,
        evaluations=tuple(case_evaluations),
    )
    evaluation_result_path = output_root / "evaluation-result.json"
    evaluation_result_payload = evaluation_result.model_dump_json(indent=2).encode(
        "utf-8"
    )
    evaluation_result_path.write_bytes(evaluation_result_payload)
    judge_inputs: tuple[JudgeInput, ...] = tuple(
        build_judge_input(
            evaluation,
            trace_evidence_refs=tuple(
                ref for step in trace.steps for ref in step.evidence_refs
            ),
        )
        for evaluation, trace in zip(case_evaluations, traces, strict=True)
    )
    judge_inputs_path = output_root / "judge-inputs.jsonl"
    judge_inputs_payload = "".join(
        f"{item.model_dump_json()}\n" for item in judge_inputs
    ).encode("utf-8")
    judge_inputs_path.write_bytes(judge_inputs_payload)
    stage_2_rule_gate_passed = all(
        item.hard_pass and item.rule_quality_pass for item in case_evaluations
    )
    real_model_calls = sum(
        step.kind == "tool" and step.tool_kind == "LLM"
        for trace in traces
        for step in trace.steps
    )
    summary = EvaluationRunSummary(
        evaluation_id=evaluation_id,
        generated_at=datetime.now(UTC),
        evaluation_mode=evaluation_mode,
        dataset_id=f"{dataset.manifest.dataset_id}-workflow",
        dataset_version=dataset.manifest.dataset_version,
        dataset_sha256=dataset.manifest.workflow_cases.sha256,
        selected_cases=len(cases),
        attempts_per_case=attempts_per_case,
        expected_runs=len(cases) * attempts_per_case,
        completed_runs=len(traces),
        passed_runs=sum(item.passed for item in run_summaries),
        trace_validation_passed=True,
        traces_file=trace_path.name,
        traces_sha256=hashlib.sha256(trace_payload).hexdigest(),
        case_results_file=case_results_path.name,
        case_results_sha256=hashlib.sha256(case_results_payload).hexdigest(),
        evaluation_result_file=evaluation_result_path.name,
        evaluation_result_sha256=hashlib.sha256(
            evaluation_result_payload
        ).hexdigest(),
        judge_inputs_file=judge_inputs_path.name,
        judge_inputs_sha256=hashlib.sha256(judge_inputs_payload).hexdigest(),
        stage_2_rule_gate_passed=stage_2_rule_gate_passed,
        real_model_calls=real_model_calls,
        estimated_cost=None,
        limitations=_run_limitations(language_model is not None),
        runs=tuple(run_summaries),
    )
    (output_root / "run-summary.json").write_text(
        summary.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return summary


def _run_limitations(model_in_the_loop: bool) -> tuple[str, ...]:
    shared = (
        "All inventory is deterministic MOCK data.",
        "Tool choice is orchestrator-controlled, not open model tool selection.",
        "Output quality uses deterministic structural rules, not an LLM Judge.",
    )
    if not model_in_the_loop:
        return (
            *shared,
            "This run does not measure real-model stability.",
            "Cost is unavailable because no priced model call occurred.",
        )
    return (
        *shared,
        "The model only extracts intent; planning, policy, and handoff stay deterministic.",
        "Case messages are rendered from a fixed template, so intent extraction is easier "
        "than free-form production phrasing.",
        "Every case carries the same hard constraints and soft preference, so constraint "
        "variety is not exercised.",
        "Cost is reported by the separate performance evaluation, not by this runner.",
    )


def _select_cases(
    dataset: LoadedEvaluationDataset,
    case_ids: tuple[str, ...] | None,
) -> tuple[WorkflowEvaluationCase, ...]:
    if case_ids is None:
        return dataset.workflow_cases
    requested = set(case_ids)
    selected = tuple(case for case in dataset.workflow_cases if case.case_id in requested)
    missing = requested - {case.case_id for case in selected}
    if missing:
        raise EvaluationRunnerError(
            "Unknown workflow case IDs: " + ", ".join(sorted(missing))
        )
    if not selected:
        raise EvaluationRunnerError("At least one workflow case must be selected")
    return selected


def _validate_trace_invariants(
    trace: EvaluationTrace,
    observation: WorkflowEvaluationObservation,
) -> None:
    expected_sequence = tuple(range(1, len(trace.steps) + 1))
    if tuple(step.sequence for step in trace.steps) != expected_sequence:
        raise EvaluationRunnerError(f"Non-contiguous trace sequence: {trace.run_id}")
    if any(step.duration_ms < 0 for step in trace.steps):
        raise EvaluationRunnerError(f"Negative trace duration: {trace.run_id}")
    tool_steps = tuple(step for step in trace.steps if step.kind == "tool")
    if len(tool_steps) != observation.tool_calls_used:
        raise EvaluationRunnerError(
            f"Tool trace count does not match task record: {trace.run_id}"
        )
    if any(step.input_hash is None or step.output_hash is None for step in trace.steps):
        raise EvaluationRunnerError(f"Missing trace hashes: {trace.run_id}")
