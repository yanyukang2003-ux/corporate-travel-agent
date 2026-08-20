"""Regression tests for the real-model workflow evaluation path.

Before this path existed, every real-model run was intent-only, so the policy,
approval, revalidation, and handoff steps had no model-in-the-loop evidence.
These tests use a scripted model so the contract is pinned without billing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_travel_agent.agent.ports import IntentExtractionResult, LLMCallMetadata
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.services.evaluation_dataset import (
    load_evaluation_dataset,
    render_workflow_case_message,
)
from corporate_travel_agent.services.evaluation_efficiency import (
    evaluate_tool_efficiency_case,
)
from corporate_travel_agent.services.evaluation_quality import WorkflowCaseEvaluation
from corporate_travel_agent.services.evaluation_runner import (
    EvaluationRunnerError,
    run_model_workflow_evaluation,
)
from corporate_travel_agent.services.evaluation_trace import EvaluationTrace
from corporate_travel_agent.services.evaluation_trajectory import (
    evaluate_trajectory_case,
    expected_tool_path,
    load_tool_registry,
)

PROJECT_ROOT = Path(__file__).parents[1]
DATASET_ROOT = PROJECT_ROOT / "data" / "evaluation" / "derived-v2"
REGISTRY_V2 = PROJECT_ROOT / "evals" / "contracts" / "tool-registry-v2.json"
SUBSET = PROJECT_ROOT / "evals" / "subsets" / "workflow-model-smoke-v1.json"
DATASET = load_evaluation_dataset(DATASET_ROOT)


class ReplayingIntentModel:
    """Returns the frozen case request, standing in for a real model."""

    prompt_version = "scripted-workflow-v1"
    model = "scripted-model"

    def __init__(self, cases) -> None:
        self.by_task = {case.case_id: case for case in cases}
        self.calls = 0

    def extract_trip_intent(
        self, message: str, *, task_id: str, traveler_id: str, context: dict
    ) -> IntentExtractionResult:
        self.calls += 1
        request = self.by_task[task_id].request
        payload = IntentExtractionSchema(
            classification="MULTI_DAY_TRIP",
            fields=TripIntentFields(
                origin=request.origin,
                destination=request.destination,
                departure_after=request.departure_after,
                arrive_by=request.arrive_by,
                return_after=request.return_after,
                return_before=request.return_before,
                hotel_check_in=request.hotel_check_in,
                hotel_check_out=request.hotel_check_out,
                client_location=None,
                hard_constraints=list(request.hard_constraints),
                soft_preferences=list(request.soft_preferences),
            ),
            provided_fields=[
                "origin",
                "destination",
                "departure_after",
                "arrive_by",
                "return_after",
                "return_before",
                "hotel_check_in",
                "hotel_check_out",
                "hard_constraints",
                "soft_preferences",
            ],
            missing_required_fields=[],
            conflicts=[],
            assumptions=[],
            confidence=0.95,
            manipulation_detected=False,
        )
        return IntentExtractionResult(
            payload=payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model=self.model,
                duration_ms=1,
                input_tokens=100,
                output_tokens=50,
            ),
        )


def _frozen_subset_ids() -> tuple[str, ...]:
    return tuple(json.loads(SUBSET.read_text(encoding="utf-8"))["case_ids"])


def test_frozen_workflow_subset_covers_every_scenario() -> None:
    ids = set(_frozen_subset_ids())
    selected = [case for case in DATASET.workflow_cases if case.case_id in ids]

    assert len(selected) == 24
    scenarios = {case.scenario for case in selected}
    assert scenarios == {case.scenario for case in DATASET.workflow_cases}


def test_rendered_message_omits_unsupported_source_asks() -> None:
    case = next(
        item for item in DATASET.workflow_cases if item.case_id in _frozen_subset_ids()
    )
    message = render_workflow_case_message(case)

    assert case.request.origin in message
    assert case.request.destination in message
    # Replaying the source query would stall every case on a conflict clarification.
    assert message not in case.source.original_query
    assert case.source.original_query not in message


def test_model_workflow_run_reaches_handoff_and_records_the_model(tmp_path) -> None:
    case = next(
        item
        for item in DATASET.workflow_cases
        if item.scenario == "COMPLIANT" and item.case_id in _frozen_subset_ids()
    )
    model = ReplayingIntentModel(DATASET.workflow_cases)
    output = tmp_path / "model-run"

    summary = run_model_workflow_evaluation(
        DATASET_ROOT,
        output,
        language_model=model,
        case_ids=(case.case_id,),
    )

    assert summary.evaluation_mode == "model_mock"
    assert summary.completed_runs == 1
    assert summary.passed_runs == 1
    assert summary.real_model_calls == 1
    assert model.calls == 1

    trace = EvaluationTrace.model_validate_json(
        (output / "traces.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert trace.fingerprint.actual_model == "scripted-model"
    assert trace.fingerprint.prompt_version == "scripted-workflow-v1"
    tool_names = [step.name for step in trace.steps if step.kind == "tool"]
    # The model call opens the run and the full provider path still executes.
    assert tool_names[0] == "llm.extract_trip_intent"
    assert "provider.create_deep_link" in tool_names
    assert trace.final.state == "READY_FOR_HANDOFF"


def test_intent_call_is_neither_a_hallucination_nor_a_redundant_call(tmp_path) -> None:
    case = next(
        item
        for item in DATASET.workflow_cases
        if item.scenario == "COMPLIANT" and item.case_id in _frozen_subset_ids()
    )
    output = tmp_path / "model-run"
    run_model_workflow_evaluation(
        DATASET_ROOT,
        output,
        language_model=ReplayingIntentModel(DATASET.workflow_cases),
        case_ids=(case.case_id,),
    )
    trace = EvaluationTrace.model_validate_json(
        (output / "traces.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    case_result = WorkflowCaseEvaluation.model_validate_json(
        (output / "case-results.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    registry = load_tool_registry(REGISTRY_V2)

    trajectory = evaluate_trajectory_case(
        case=case, trace=trace, case_result=case_result, registry=registry
    )
    assert trajectory.trajectory_pass is True
    assert trajectory.findings == ()

    efficiency = evaluate_tool_efficiency_case(
        case=case,
        trace=trace,
        task_result=case_result,
        trajectory_result=trajectory,
    )
    intent_call = next(
        item for item in efficiency.calls if item.tool_name == "llm.extract_trip_intent"
    )
    assert intent_call.invalid is False
    assert intent_call.redundant is False
    assert intent_call.no_gain is False
    assert "intent_structured" in intent_call.useful_signals
    assert efficiency.minimal_path_match is True


def test_expected_path_only_includes_the_model_step_in_model_mode() -> None:
    case = DATASET.workflow_cases[0]

    assert expected_tool_path(case)[0].startswith("provider.")
    assert expected_tool_path(case, model_in_the_loop=True)[0] == "llm.extract_trip_intent"


def test_model_run_requires_a_language_model(tmp_path) -> None:
    with pytest.raises(EvaluationRunnerError, match="language model"):
        run_model_workflow_evaluation(
            DATASET_ROOT, tmp_path / "no-model", language_model=None
        )
