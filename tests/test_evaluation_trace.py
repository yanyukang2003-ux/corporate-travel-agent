from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from corporate_travel_agent.agent.ports import WorkflowTraceEvent
from corporate_travel_agent.services.evaluation_dataset import (
    load_evaluation_dataset,
    run_workflow_evaluation_case,
)
from corporate_travel_agent.services.evaluation_quality import (
    EvaluationResult,
    JudgeInput,
    WorkflowCaseEvaluation,
    evaluate_workflow_case,
)
from corporate_travel_agent.services.evaluation_runner import (
    EvaluationRunnerError,
    EvaluationRunSummary,
    run_deterministic_workflow_evaluation,
)
from corporate_travel_agent.services.evaluation_trace import (
    EvaluationTrace,
    EvaluationTraceRecorder,
    TraceFinal,
    TraceFingerprint,
)

DATASET_ROOT = Path(__file__).parents[1] / "data" / "evaluation" / "derived-v2"
DATASET = load_evaluation_dataset(DATASET_ROOT)


def _fingerprint() -> TraceFingerprint:
    return TraceFingerprint(
        project_version="0.1.0",
        code_revision=None,
        dataset_id="test-dataset",
        dataset_version="1",
        dataset_sha256="a" * 64,
        prompt_version=None,
        requested_model=None,
        actual_model=None,
        runner_version="agent-eval-runner-v1",
        price_table_version=None,
    )


def test_trace_accepts_controlled_test_order_evaluation_mode() -> None:
    recorder = EvaluationTraceRecorder(
        run_id="run-test-order-mode",
        case_id="case-test-order-mode",
        attempt=1,
        evaluation_mode="model_live_provider_test_order",
        fingerprint=_fingerprint(),
        tool_choice_exposure="orchestrator_controlled",
    )

    trace = recorder.finish(
        TraceFinal(
            state="HANDED_OFF",
            policy_outcome=None,
            booking_allowed=False,
            result_refs=(),
            user_response_hash=None,
            failure_reason=None,
        )
    )

    assert trace.evaluation_mode == "model_live_provider_test_order"


def test_trace_recorder_redacts_sensitive_inputs_and_retains_hashes() -> None:
    recorder = EvaluationTraceRecorder(
        run_id="run-redaction",
        case_id="case-redaction",
        attempt=1,
        evaluation_mode="deterministic_mock",
        fingerprint=_fingerprint(),
        tool_choice_exposure="orchestrator_controlled",
    )
    recorder.record(
        WorkflowTraceEvent(
            kind="tool",
            name="provider.example",
            status="success",
            started_at=datetime.now(UTC),
            duration_ms=1.25,
            state_before="SEARCHING",
            state_after="SEARCHING",
            input_value={
                "employee_id": "SYN-E0001",
                "email": "person@example.com",
                "city": "Shanghai",
            },
            output_value={"ref_id": "SAFE-REF"},
            evidence_refs=("SAFE-REF",),
            tool_kind="PROVIDER",
            tool_call_sequence=1,
        )
    )
    trace = recorder.finish(
        TraceFinal(
            state="WAITING_FOR_USER",
            policy_outcome=None,
            booking_allowed=False,
            result_refs=("SAFE-REF",),
            user_response_hash=None,
            failure_reason=None,
        )
    )

    payload = trace.model_dump_json()
    assert "SYN-E0001" not in payload
    assert "person@example.com" not in payload
    assert "Shanghai" in payload
    assert trace.steps[0].input_hash is not None
    assert trace.steps[0].output_hash is not None


def test_workflow_trace_has_one_tool_step_per_recorded_tool_call() -> None:
    case = next(case for case in DATASET.workflow_cases if case.scenario == "COMPLIANT")
    recorder = EvaluationTraceRecorder(
        run_id="run-workflow",
        case_id=case.case_id,
        attempt=1,
        evaluation_mode="deterministic_mock",
        fingerprint=_fingerprint(),
        tool_choice_exposure="orchestrator_controlled",
    )

    observation = run_workflow_evaluation_case(case, trace_observer=recorder)
    trace = recorder.finish(
        TraceFinal(
            state=observation.final_state.value,
            policy_outcome=observation.selected_policy_outcome.value,
            booking_allowed=observation.booking_intent_created,
            result_refs=observation.selected_inventory_refs,
            user_response_hash=None,
            failure_reason=observation.failure_reason,
        )
    )

    tool_steps = [step for step in trace.steps if step.kind == "tool"]
    assert len(tool_steps) == observation.tool_calls_used
    assert [step.tool_call_sequence for step in tool_steps] == list(
        range(1, observation.tool_calls_used + 1)
    )
    assert all(step.duration_ms >= 0 for step in tool_steps)
    assert all(step.state_before and step.state_after for step in trace.steps)
    assert any(step.kind == "state_transition" for step in trace.steps)


def test_runner_writes_strict_jsonl_and_refuses_overwrite(tmp_path: Path) -> None:
    selected_ids = tuple(case.case_id for case in DATASET.workflow_cases[:2])
    output = tmp_path / "trace-run"

    summary = run_deterministic_workflow_evaluation(
        DATASET_ROOT,
        output,
        case_ids=selected_ids,
    )

    assert summary.selected_cases == 2
    assert summary.completed_runs == 2
    assert summary.real_model_calls == 0
    assert summary.estimated_cost is None
    assert summary.stage_2_rule_gate_passed is True
    lines = (output / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    traces = [EvaluationTrace.model_validate_json(line) for line in lines]
    assert len(traces) == 2
    assert all(
        trace.fingerprint.dataset_sha256 == DATASET.manifest.workflow_cases.sha256
        for trace in traces
    )
    assert all(trace.final.user_response_hash is not None for trace in traces)
    case_results = [
        WorkflowCaseEvaluation.model_validate_json(line)
        for line in (output / "case-results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(item.hard_pass and item.rule_quality_pass for item in case_results)
    result = EvaluationResult.model_validate_json(
        (output / "evaluation-result.json").read_text(encoding="utf-8")
    )
    assert result.metrics["task_pass_rate"].value == 1.0
    assert result.metrics["rule_output_completeness_rate"].value == 1.0
    assert result.metrics["judge_quality_score"].value is None
    assert result.metrics["judge_quality_score"].status == "unavailable"
    assert result.gate_status == "not_evaluated"
    judge_inputs = [
        JudgeInput.model_validate_json(line)
        for line in (output / "judge-inputs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(judge_inputs) == 2
    assert all(item.candidate_identity_blinded for item in judge_inputs)
    assert all(item.rubric_id == "output-quality-v1" for item in judge_inputs)
    json.loads((output / "run-summary.json").read_text(encoding="utf-8"))
    EvaluationRunSummary.model_validate_json(
        (output / "run-summary.json").read_text(encoding="utf-8")
    )

    with pytest.raises(EvaluationRunnerError, match="already exists"):
        run_deterministic_workflow_evaluation(
            DATASET_ROOT,
            output,
            case_ids=selected_ids,
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "COMPLIANT",
        "PREFERENCE_CONFLICT",
        "REQUIRES_APPROVAL",
        "NO_FEASIBLE_OPTION",
        "REVALIDATION_CHANGED",
        "PROVIDER_FAILURE",
    ],
)
def test_rule_quality_projection_covers_every_workflow_scenario(scenario: str) -> None:
    case = next(case for case in DATASET.workflow_cases if case.scenario == scenario)
    observation = run_workflow_evaluation_case(case)

    evaluation = evaluate_workflow_case(
        run_id=f"run-{scenario.lower()}",
        case=case,
        observation=observation,
    )

    assert evaluation.hard_pass is True
    assert evaluation.rule_quality_pass is True
    assert evaluation.rule_quality_score == 1.0
    assert evaluation.judge_quality_score is None
    assert evaluation.judge_status == "unavailable"
    assert evaluation.user_output.next_action
    assert evaluation.user_output.booking_boundary_notice == "NO_AUTONOMOUS_BOOKING_OR_PAYMENT"
    assert all(option.evidence_refs for option in evaluation.user_output.options)
