"""评测什么：真实模型意图抽取的稳定性与预检子集运行器。"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corporate_travel_agent import __version__
from corporate_travel_agent.agent.ports import LanguageModelPort
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_dataset import (
    IntentEvaluationCase,
    IntentEvaluationObservation,
    load_evaluation_dataset,
)
from corporate_travel_agent.services.evaluation_runner import (
    CaseRunSummary,
    EvaluationRunnerError,
    EvaluationRunSummary,
)
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationMode,
    EvaluationTrace,
    EvaluationTraceRecorder,
    TraceFinal,
    TraceFingerprint,
)

MODEL_RUNNER_VERSION = "model-intent-eval-runner-v1"
MODEL_PREFLIGHT_RUNNER_VERSION = "model-intent-preflight-runner-v1"
SHA256_PATTERN = r"^[a-f0-9]{64}$"


class ModelRunnerModel(BaseModel):
    """模型意图运行器 Pydantic 基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class IntentModelSubset(ModelRunnerModel):
    """意图模型评测子集定义。"""
    schema_version: Literal[1]
    subset_id: Literal["intent-model-smoke-v1"]
    status: Literal["frozen"]
    source_dataset_id: Literal["corporate-travel-derived-v2-intent"]
    source_dataset_version: Literal["2"]
    source_dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    records: Literal[24]
    selection: str = Field(min_length=1)
    languages: dict[str, int]
    cohorts: dict[str, int]
    scenarios: dict[str, int]
    case_ids: tuple[str, ...]

    @model_validator(mode="after")
    def counts_match_case_ids(self) -> IntentModelSubset:
        if len(self.case_ids) != self.records:
            raise ValueError("subset record count does not match case IDs")
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("subset case IDs must be unique")
        return self


class IntentModelPreflightSubset(ModelRunnerModel):
    """意图模型预检子集定义。"""
    schema_version: Literal[1]
    subset_id: Literal["intent-model-preflight-v1"]
    status: Literal["frozen"]
    source_dataset_id: Literal["corporate-travel-derived-v2-intent"]
    source_dataset_version: Literal["2"]
    source_dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    parent_subset_id: Literal["intent-model-smoke-v1"]
    parent_subset_sha256: str = Field(pattern=SHA256_PATTERN)
    records: Literal[2]
    selection: str = Field(min_length=1)
    languages: dict[str, int]
    cohorts: dict[str, int]
    scenarios: dict[str, int]
    case_ids: tuple[str, ...]

    @model_validator(mode="after")
    def counts_match_case_ids(self) -> IntentModelPreflightSubset:
        if len(self.case_ids) != self.records:
            raise ValueError("preflight subset record count does not match case IDs")
        if len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("preflight subset case IDs must be unique")
        return self


class IntentModelCaseResult(ModelRunnerModel):
    """单条意图模型用例结果。"""
    run_id: str
    case_id: str
    attempt: int = Field(ge=1)
    expected_classification: str
    actual_classification: str
    expected_missing_fields: tuple[str, ...] | None
    actual_missing_fields: tuple[str, ...]
    expected_transport_preferences: tuple[str, ...] | None
    actual_transport_preferences: tuple[str, ...]
    clarification_expected: bool
    clarification_observed: bool
    unsupported_rejection_expected: bool
    unsupported_rejection_observed: bool
    provider_calls_before_clarification: int = Field(ge=0)
    inventory_hallucinated: bool
    passed: bool
    mismatches: tuple[str, ...]


def load_intent_model_subset(
    subset_path: str | Path,
    *,
    dataset_directory: str | Path,
) -> tuple[IntentModelSubset, tuple[IntentEvaluationCase, ...], str]:
    """加载意图模型稳定性评测子集。"""
    path = Path(subset_path).expanduser().resolve()
    payload = path.read_bytes()
    subset = IntentModelSubset.model_validate_json(payload)
    dataset = load_evaluation_dataset(dataset_directory)
    if dataset.manifest.intent_cases.sha256 != subset.source_dataset_sha256:
        raise EvaluationRunnerError("intent subset source fingerprint does not match D2")
    by_id = {case.case_id: case for case in dataset.intent_cases}
    missing = tuple(case_id for case_id in subset.case_ids if case_id not in by_id)
    if missing:
        raise EvaluationRunnerError(
            "intent subset references missing cases: " + ", ".join(missing)
        )
    cases = tuple(by_id[case_id] for case_id in subset.case_ids)
    expected_counts = {
        "languages": dict(Counter(case.language for case in cases)),
        "cohorts": dict(Counter(case.cohort for case in cases)),
        "scenarios": dict(Counter(case.scenario for case in cases)),
    }
    for field, actual in expected_counts.items():
        if getattr(subset, field) != actual:
            raise EvaluationRunnerError(f"intent subset {field} counts do not match D2")
    return subset, cases, _sha256(payload)


def load_intent_model_preflight_subset(
    subset_path: str | Path,
    *,
    dataset_directory: str | Path,
    parent_subset_path: str | Path,
) -> tuple[IntentModelPreflightSubset, tuple[IntentEvaluationCase, ...], str]:
    """加载意图模型预检子集。"""
    path = Path(subset_path).expanduser().resolve()
    payload = path.read_bytes()
    subset = IntentModelPreflightSubset.model_validate_json(payload)
    parent, _, parent_sha256 = load_intent_model_subset(
        parent_subset_path,
        dataset_directory=dataset_directory,
    )
    if parent_sha256 != subset.parent_subset_sha256:
        raise EvaluationRunnerError("preflight parent subset fingerprint does not match")
    if not set(subset.case_ids).issubset(parent.case_ids):
        raise EvaluationRunnerError("preflight cases must be selected from the frozen parent")
    dataset = load_evaluation_dataset(dataset_directory)
    if dataset.manifest.intent_cases.sha256 != subset.source_dataset_sha256:
        raise EvaluationRunnerError("preflight subset source fingerprint does not match D2")
    by_id = {case.case_id: case for case in dataset.intent_cases}
    missing = tuple(case_id for case_id in subset.case_ids if case_id not in by_id)
    if missing:
        raise EvaluationRunnerError(
            "preflight subset references missing cases: " + ", ".join(missing)
        )
    cases = tuple(by_id[case_id] for case_id in subset.case_ids)
    expected_counts = {
        "languages": dict(Counter(case.language for case in cases)),
        "cohorts": dict(Counter(case.cohort for case in cases)),
        "scenarios": dict(Counter(case.scenario for case in cases)),
    }
    for field, actual in expected_counts.items():
        if getattr(subset, field) != actual:
            raise EvaluationRunnerError(f"preflight subset {field} counts do not match D2")
    return subset, cases, _sha256(payload)


def run_model_intent_stability_evaluation(
    *,
    dataset_directory: str | Path,
    subset_path: str | Path,
    output_directory: str | Path,
    language_model: LanguageModelPort,
    attempts_per_case: int = 3,
    code_revision: str | None = None,
    price_table_version: str | None = None,
) -> EvaluationRunSummary:
    """运行意图模型稳定性评测。"""
    if attempts_per_case != 3:
        raise EvaluationRunnerError(
            "the frozen real-model stability protocol requires exactly 3 attempts"
        )
    subset, cases, subset_sha256 = load_intent_model_subset(
        subset_path,
        dataset_directory=dataset_directory,
    )
    return _run_model_intent_evaluation(
        cases=cases,
        output_directory=output_directory,
        language_model=language_model,
        attempts_per_case=attempts_per_case,
        dataset_id=subset.subset_id,
        dataset_sha256=subset_sha256,
        evaluation_mode="model_mock",
        runner_version=MODEL_RUNNER_VERSION,
        code_revision=code_revision,
        price_table_version=price_table_version,
        limitations=(
            "The language model extracts intent; workflow planning and tool selection remain "
            "orchestrator-controlled.",
            "Inventory and provider calls are deterministic Mock data, not live providers.",
            "Cost is intentionally left null here and is calculated only by the Stage 6 "
            "aggregator with a versioned configured price table.",
            "The 24-case subset emphasizes scenario coverage and is not representative of "
            "production traffic proportions.",
        ),
    )


def run_model_intent_preflight_evaluation(
    *,
    dataset_directory: str | Path,
    subset_path: str | Path,
    parent_subset_path: str | Path,
    output_directory: str | Path,
    language_model: LanguageModelPort,
    code_revision: str | None = None,
    price_table_version: str | None = None,
) -> EvaluationRunSummary:
    """运行意图模型预检评测。"""
    subset, cases, subset_sha256 = load_intent_model_preflight_subset(
        subset_path,
        dataset_directory=dataset_directory,
        parent_subset_path=parent_subset_path,
    )
    return _run_model_intent_evaluation(
        cases=cases,
        output_directory=output_directory,
        language_model=language_model,
        attempts_per_case=1,
        dataset_id=subset.subset_id,
        dataset_sha256=subset_sha256,
        evaluation_mode="real_llm_mock_provider",
        runner_version=MODEL_PREFLIGHT_RUNNER_VERSION,
        code_revision=code_revision,
        price_table_version=price_table_version,
        limitations=(
            "This is a two-call qualification preflight, not a quality or stability score.",
            "The real language model only extracts intent; planning and tool selection remain "
            "orchestrator-controlled.",
            "Inventory and provider calls are deterministic Mock data, not live providers.",
            "The fixed two-case sample checks bilingual structured-output plumbing and cannot "
            "represent production traffic proportions.",
        ),
    )


def _run_model_intent_evaluation(
    *,
    cases: tuple[IntentEvaluationCase, ...],
    output_directory: str | Path,
    language_model: LanguageModelPort,
    attempts_per_case: int,
    dataset_id: str,
    dataset_sha256: str,
    evaluation_mode: EvaluationMode,
    runner_version: str,
    code_revision: str | None,
    price_table_version: str | None,
    limitations: tuple[str, ...],
) -> EvaluationRunSummary:
    output_root = Path(output_directory).expanduser().resolve()
    if output_root.exists():
        raise EvaluationRunnerError(f"Output directory already exists: {output_root}")
    output_root.mkdir(parents=True)

    model_name = str(getattr(language_model, "model", "unknown-model"))
    prompt_version = str(getattr(language_model, "prompt_version", "unknown-prompt"))
    fingerprint = TraceFingerprint(
        project_version=__version__,
        code_revision=code_revision,
        dataset_id=dataset_id,
        dataset_version="1",
        dataset_sha256=dataset_sha256,
        prompt_version=prompt_version,
        requested_model=model_name,
        actual_model=model_name,
        runner_version=runner_version,
        price_table_version=price_table_version,
    )
    evaluation_id = f"eval-{uuid4()}"
    traces: list[EvaluationTrace] = []
    results: list[IntentModelCaseResult] = []
    summaries: list[CaseRunSummary] = []
    observed_at = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    for case in cases:
        for attempt in range(1, attempts_per_case + 1):
            run_id = f"run-{uuid4()}"
            recorder = EvaluationTraceRecorder(
                run_id=run_id,
                case_id=case.case_id,
                attempt=attempt,
                evaluation_mode=evaluation_mode,
                fingerprint=fingerprint,
                tool_choice_exposure="orchestrator_controlled",
            )
            workflow, _ = build_demo_system(
                language_model=language_model,
                clock=lambda observed_at=observed_at: observed_at,
                trace_observer=recorder,
            )
            task = workflow.create_task_from_message(
                case.message,
                traveler_id="E1001",
                task_id=f"intent-model-eval-{case.case_id}",
            )
            observation = _intent_observation(case, task)
            result = _evaluate_intent_result(run_id, attempt, case, observation)
            refs = tuple(
                dict.fromkeys(ref for option in task.options for ref in option.inventory_refs)
            )
            trace = recorder.finish(
                TraceFinal(
                    state=task.state.value,
                    policy_outcome=None,
                    booking_allowed=task.booking_intent is not None,
                    result_refs=refs,
                    user_response_hash=stable_hash(
                        {
                            "state": task.state.value,
                            "classification": observation.actual_classification,
                            "missing_fields": observation.actual_missing_fields,
                            "preferences": observation.actual_transport_preferences,
                            "conflicts": observation.actual_conflicts,
                        }
                    ),
                    failure_reason=task.failure,
                )
            )
            traces.append(trace)
            results.append(result)
            summaries.append(
                CaseRunSummary(
                    run_id=run_id,
                    case_id=case.case_id,
                    attempt=attempt,
                    passed=result.passed,
                    mismatches=result.mismatches,
                    trace_steps=len(trace.steps),
                    tool_steps=sum(step.kind == "tool" for step in trace.steps),
                    rule_quality_passed=result.passed,
                    rule_quality_score=float(result.passed),
                )
            )

    trace_path = output_root / "traces.jsonl"
    trace_payload = "".join(f"{trace.model_dump_json()}\n" for trace in traces).encode()
    trace_path.write_bytes(trace_payload)
    result_path = output_root / "intent-case-results.jsonl"
    result_payload = "".join(f"{item.model_dump_json()}\n" for item in results).encode()
    result_path.write_bytes(result_payload)
    model_calls = sum(
        step.kind == "tool" and step.tool_kind == "LLM"
        for trace in traces
        for step in trace.steps
    )
    summary = EvaluationRunSummary(
        evaluation_id=evaluation_id,
        generated_at=datetime.now(UTC),
        evaluation_mode=evaluation_mode,
        dataset_id=dataset_id,
        dataset_version="1",
        dataset_sha256=dataset_sha256,
        selected_cases=len(cases),
        attempts_per_case=attempts_per_case,
        expected_runs=len(cases) * attempts_per_case,
        completed_runs=len(traces),
        passed_runs=sum(item.passed for item in summaries),
        trace_validation_passed=True,
        traces_file=trace_path.name,
        traces_sha256=_sha256(trace_payload),
        case_results_file=result_path.name,
        case_results_sha256=_sha256(result_payload),
        evaluation_result_file=None,
        evaluation_result_sha256=None,
        judge_inputs_file=None,
        judge_inputs_sha256=None,
        stage_2_rule_gate_passed=None,
        real_model_calls=model_calls,
        estimated_cost=None,
        limitations=limitations,
        runs=tuple(summaries),
    )
    (output_root / "run-summary.json").write_text(
        summary.model_dump_json(indent=2), encoding="utf-8"
    )
    return summary


def _intent_observation(case: IntentEvaluationCase, task) -> IntentEvaluationObservation:
    provider_calls = sum(record.tool_kind == "PROVIDER" for record in task.tool_calls)
    preferences = tuple(task.intent_fields.get("soft_preferences") or ())
    return IntentEvaluationObservation(
        case_id=case.case_id,
        entrypoint="legacy",
        expected_classification=case.expected.classification,
        actual_classification=str(task.metadata.get("intent_classification", "UNCLASSIFIED")),
        expected_missing_fields=case.expected.missing_fields,
        actual_missing_fields=tuple(task.missing_required_fields),
        clarification_expected=case.expected.must_clarify_before_search,
        clarification_observed=task.state is TaskState.NEEDS_CLARIFICATION,
        expected_transport_preferences=case.expected.expected_transport_preferences,
        actual_transport_preferences=preferences,
        unsupported_constraint_rejection_expected=(
            case.expected.must_reject_unsupported_constraints
        ),
        unsupported_constraints_rejected=(
            bool(task.intent_conflicts) or task.state is TaskState.OUT_OF_SCOPE
        ),
        actual_conflicts=tuple(task.intent_conflicts),
        provider_calls_before_clarification=provider_calls,
        inventory_hallucinated=bool(
            task.options and not any(record.tool_kind == "PROVIDER" for record in task.tool_calls)
        ),
    )


def _evaluate_intent_result(
    run_id: str,
    attempt: int,
    case: IntentEvaluationCase,
    observation: IntentEvaluationObservation,
) -> IntentModelCaseResult:
    mismatches: list[str] = []
    if observation.actual_classification != observation.expected_classification:
        mismatches.append("classification")
    if observation.expected_missing_fields is not None and set(
        observation.actual_missing_fields
    ) != set(observation.expected_missing_fields):
        mismatches.append("missing_fields")
    if observation.clarification_observed != observation.clarification_expected:
        mismatches.append("clarification")
    if observation.expected_transport_preferences is not None and set(
        observation.actual_transport_preferences
    ) != set(observation.expected_transport_preferences):
        mismatches.append("transport_preferences")
    if (
        observation.unsupported_constraint_rejection_expected
        and not observation.unsupported_constraints_rejected
    ):
        mismatches.append("unsupported_constraint_rejection")
    if (
        observation.clarification_expected
        and observation.provider_calls_before_clarification
    ):
        mismatches.append("premature_provider_call")
    if observation.inventory_hallucinated:
        mismatches.append("inventory_hallucination")
    return IntentModelCaseResult(
        run_id=run_id,
        case_id=case.case_id,
        attempt=attempt,
        expected_classification=observation.expected_classification,
        actual_classification=observation.actual_classification,
        expected_missing_fields=observation.expected_missing_fields,
        actual_missing_fields=observation.actual_missing_fields,
        expected_transport_preferences=observation.expected_transport_preferences,
        actual_transport_preferences=observation.actual_transport_preferences,
        clarification_expected=observation.clarification_expected,
        clarification_observed=observation.clarification_observed,
        unsupported_rejection_expected=(
            observation.unsupported_constraint_rejection_expected
        ),
        unsupported_rejection_observed=observation.unsupported_constraints_rejected,
        provider_calls_before_clarification=(
            observation.provider_calls_before_clarification
        ),
        inventory_hallucinated=observation.inventory_hallucinated,
        passed=not mismatches,
        mismatches=tuple(mismatches),
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
