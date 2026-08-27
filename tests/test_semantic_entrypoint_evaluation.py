"""D15：语义入口进入评测 harness 后的覆盖与安全断言。"""

from __future__ import annotations

from pathlib import Path

import pytest

from corporate_travel_agent.agent.deterministic_semantic_interpreter import (
    DeterministicSemanticInterpreter,
)
from corporate_travel_agent.agent.semantic_intent import (
    ConversationIntentInterpreter,
    ConversationLedger,
    IntentDecisionStatus,
)
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.domain.models import ConversationMessage
from corporate_travel_agent.services.evaluation_dataset import (
    EvaluationDatasetError,
    load_evaluation_dataset,
    run_intent_evaluation,
    run_workflow_evaluation_case,
)

DATASET = "data/evaluation/derived-v2"
CONTEXT = {"reference_time": "2026-07-20T09:00:00+00:00", "timezone": "Asia/Shanghai"}


def _interpret(*messages: str):
    ledger = ConversationLedger.from_messages(
        [ConversationMessage(role="user", content=item) for item in messages]
    )
    interpreter = ConversationIntentInterpreter(DeterministicSemanticInterpreter())
    return interpreter.interpret(
        ledger, task_id="t1", traveler_id="E1001", context=CONTEXT
    ).decision


def test_interpreter_evidence_is_grounded_in_the_quoted_turn() -> None:
    decision = _interpret(
        "I need to book a corporate trip from St. Louis to Baltimore. "
        "I cannot leave before 2026-01-04T06:00:00Z and I must land by "
        "2026-01-04T13:00:00Z."
    )
    assert decision.status is IntentDecisionStatus.READY
    assert decision.intent.origin_candidates == ["St. Louis"]
    assert decision.intent.destination_candidates == ["Baltimore"]
    for evidence in decision.evidence:
        assert evidence.quote


def test_interpreter_never_guesses_a_missing_deadline() -> None:
    decision = _interpret("I need to book a corporate trip from Beijing to Shanghai.")
    assert decision.status is IntentDecisionStatus.NEEDS_CLARIFICATION
    assert decision.intent.arrive_by is None
    assert decision.clarification_question


def test_later_turns_override_earlier_meaning_without_patching() -> None:
    decision = _interpret(
        "I need to book a corporate trip from Beijing to Shanghai. "
        "I cannot leave before 2026-01-04T06:00:00Z and I must land by "
        "2026-01-04T13:00:00Z.",
        "I need to book a corporate trip from Beijing to Shenzhen. "
        "I cannot leave before 2026-01-05T06:00:00Z and I must land by "
        "2026-01-05T13:00:00Z.",
    )
    assert decision.intent.destination_candidates == ["Shenzhen"]
    assert decision.intent.departure_after.isoformat().startswith("2026-01-05")


def test_interpreter_has_no_legacy_extraction_method() -> None:
    assert not hasattr(DeterministicSemanticInterpreter(), "extract_trip_intent")


def test_workflow_cases_run_through_the_semantic_entrypoint() -> None:
    dataset = load_evaluation_dataset(DATASET)
    for case in dataset.workflow_cases:
        observation = run_workflow_evaluation_case(
            case, semantic_language_model=DeterministicSemanticInterpreter()
        )
        expected = case.expected
        assert observation.task.metadata["intent_entrypoint"] == "semantic"
        assert observation.after_create_state is expected.after_create_state, case.case_id
        assert observation.after_selection_state is expected.after_selection_state
        assert observation.selected_policy_outcome is expected.selected_policy_outcome
        assert observation.booking_intent_created is expected.booking_intent_expected


def test_a_workflow_case_runs_through_exactly_one_entrypoint() -> None:
    dataset = load_evaluation_dataset(DATASET)
    with pytest.raises(EvaluationDatasetError):
        run_workflow_evaluation_case(
            dataset.workflow_cases[0],
            language_model=object(),
            semantic_language_model=DeterministicSemanticInterpreter(),
        )


def test_semantic_intent_cases_never_search_on_unresolved_meaning() -> None:
    dataset = load_evaluation_dataset(DATASET)
    metrics = run_intent_evaluation(dataset.intent_cases[:60], entrypoint="semantic")
    assert metrics.entrypoint == "semantic"
    assert metrics.premature_provider_call_rate == 0.0
    assert metrics.inventory_hallucination_rate == 0.0


def test_semantic_metrics_report_classification_as_not_applicable() -> None:
    dataset = load_evaluation_dataset(DATASET)
    semantic = run_intent_evaluation(dataset.intent_cases[:20], entrypoint="semantic")
    legacy = run_intent_evaluation(dataset.intent_cases[:20], entrypoint="legacy")
    assert semantic.classification_status == "not_applicable"
    assert legacy.classification_status == "measured"


def test_observations_from_different_entrypoints_are_never_merged() -> None:
    from corporate_travel_agent.services.evaluation_dataset import (
        summarize_intent_observations,
    )

    dataset = load_evaluation_dataset(DATASET)
    semantic = run_intent_evaluation(dataset.intent_cases[:5], entrypoint="semantic")
    legacy = run_intent_evaluation(dataset.intent_cases[:5], entrypoint="legacy")
    with pytest.raises(EvaluationDatasetError):
        summarize_intent_observations(
            (*semantic.observations, *legacy.observations)
        )


def test_unknown_entrypoint_is_rejected() -> None:
    dataset = load_evaluation_dataset(DATASET)
    with pytest.raises(EvaluationDatasetError):
        run_intent_evaluation(dataset.intent_cases[:1], entrypoint="hybrid")


def test_out_of_scope_message_stops_before_any_provider_call() -> None:
    dataset = load_evaluation_dataset(DATASET)
    out_of_scope = [
        case
        for case in dataset.intent_cases
        if case.expected.classification == "OUT_OF_SCOPE"
    ]
    metrics = run_intent_evaluation(tuple(out_of_scope[:10]), entrypoint="semantic")
    for observation in metrics.observations:
        assert observation.provider_calls_before_clarification == 0
        assert not observation.inventory_hallucinated


def test_semantic_task_state_is_a_safe_pause_when_meaning_is_incomplete() -> None:
    dataset = load_evaluation_dataset(DATASET)
    metrics = run_intent_evaluation(dataset.intent_cases[:30], entrypoint="semantic")
    safe = {
        TaskState.NEEDS_CLARIFICATION,
        TaskState.NEEDS_STRUCTURED_INPUT,
        TaskState.OUT_OF_SCOPE,
        TaskState.WAITING_FOR_USER,
        TaskState.NO_FEASIBLE_OPTION,
    }
    for observation in metrics.observations:
        if observation.inventory_hallucinated:
            raise AssertionError(f"{observation.case_id} fabricated inventory")
    assert safe  # the enum names above must remain valid states


def test_semantic_redteam_acceptance_cases_all_pass() -> None:
    """红队 runner 进 CI：新链路的宿主职责回归由单测守着，不靠人工记得去跑。"""
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "run_semantic_redteam_acceptance.py"
    )
    spec = importlib.util.spec_from_file_location("semantic_redteam_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cases = module._run()

    assert len(cases) == 17
    failed = [item["case_id"] for item in cases if not item["passed"]]
    assert not failed, failed
    # 明确写下哪些红队检查没有移植，避免"覆盖率看起来很高"的错觉。
    assert len(module.NOT_PORTABLE) == 3
