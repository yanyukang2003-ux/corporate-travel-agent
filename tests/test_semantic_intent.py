from __future__ import annotations

from collections import deque
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from corporate_travel_agent.agent.openai_adapter import (
    OpenAIResponsesLanguageModel,
    OpenAISemanticIntentLanguageModel,
)
from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.agent.ports import (
    IntentExtractionResult,
    IntentInterpretationResult,
    LLMCallMetadata,
)
from corporate_travel_agent.agent.schemas import IntentExtractionSchema, TripIntentFields
from corporate_travel_agent.agent.search_command import compile_search_command
from corporate_travel_agent.agent.semantic_intent import (
    ConversationLedger,
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
)
from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement, TaskState
from corporate_travel_agent.domain.models import ConversationMessage
from corporate_travel_agent.services.locations import CityNormalizer

SHANGHAI = ZoneInfo("Asia/Shanghai")


class ScriptedSemanticModel:
    prompt_version = "semantic-scripted-v1"

    def __init__(self, decisions: list[IntentDecision]) -> None:
        self.decisions = deque(decisions)
        self.conversations: list[str] = []
        self.extract_calls = 0

    def interpret_trip_intent(
        self,
        conversation: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, object],
    ) -> IntentInterpretationResult:
        del task_id, traveler_id, context
        self.conversations.append(conversation)
        return IntentInterpretationResult(
            decision=self.decisions.popleft(),
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="semantic-scripted",
                duration_ms=1,
                evidence_contract_version="conversation-turn-v1",
            ),
        )

    def extract_trip_intent(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        self.extract_calls += 1
        raise AssertionError("semantic entrypoint must not call legacy extraction")

    def propose_search_adjustment(self, *_: object) -> None:
        return None

    def explain_verified_options(self, *_: object) -> dict[str, str]:
        return {}


def _ready_intent(**overrides: object) -> SemanticIntent:
    values: dict[str, object] = {
        "summary": "从北京到上海参加会议，8月5日出发，8月6日10点前到达",
        "origin_candidates": ["Beijing"],
        "destination_candidates": ["Shanghai"],
        "departure_after": datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI),
        "arrive_by": datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI),
        "return_after": None,
        "return_before": None,
        "booking_scope": BookingScope.OUTBOUND_ONLY,
        "lodging_requirement": LodgingRequirement.NOT_REQUIRED,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "client_location": None,
        "hard_constraints": ["arrive_before_meeting"],
        "soft_preferences": [],
        "alternatives": [],
        "conditions": [],
        "uncertainties": [],
    }
    values.update(overrides)
    return SemanticIntent(**values)


def _decision(intent: SemanticIntent, **overrides: object) -> IntentDecision:
    values: dict[str, object] = {
        "status": IntentDecisionStatus.READY,
        "intent": intent,
        "clarification_question": None,
        "conflicts": [],
        "unsupported_reasons": [],
        "assumptions": [],
        "evidence": [
            EvidenceRef(turn_index=0, field="origin", quote="北京"),
            EvidenceRef(turn_index=0, field="destination", quote="上海"),
        ],
        "confidence": 0.96,
        "manipulation_detected": False,
    }
    values.update(overrides)
    return IntentDecision(**values)


def test_conversation_ledger_preserves_raw_turns_and_stable_indices() -> None:
    ledger = ConversationLedger.from_messages(
        [
            ConversationMessage(role="user", content="下周从北京去上海"),
            ConversationMessage(role="assistant", content="哪一天出发？"),
            ConversationMessage(role="user", content="改成周五去伦敦"),
        ]
    )

    assert [turn.turn_index for turn in ledger.turns] == [0, 1, 2]
    assert ledger.latest_user_message == "改成周五去伦敦"
    assert "[turn:0 role:user] 下周从北京去上海" in ledger.render()
    assert "[turn:2 role:user] 改成周五去伦敦" in ledger.render()


def test_compile_refuses_to_silently_choose_an_alternative_origin() -> None:
    semantic = _ready_intent(
        origin_candidates=["Beijing", "Shanghai"],
        destination_candidates=["Philadelphia"],
        alternatives=["从北京或上海出发"],
        uncertainties=["出发城市尚未确定"],
    )
    decision = _decision(
        semantic,
        status=IntentDecisionStatus.NEEDS_CLARIFICATION,
        clarification_question="请确认从北京还是上海出发？",
    )

    compiled = compile_search_command(
        decision,
        task_id="alternative-origin",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.command is None
    assert compiled.clarification_question == "请确认从北京还是上海出发？"
    assert "origin" in compiled.missing


def test_compile_ready_semantics_into_validated_command_without_defaults() -> None:
    compiled = compile_search_command(
        _decision(_ready_intent()),
        task_id="ready-trip",
        traveler_id="E1001",
        version=2,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert compiled.ready
    assert compiled.command is not None
    request = compiled.command.request
    assert request.origin == "Beijing"
    assert request.destination == "Shanghai"
    assert request.version == 2
    assert request.booking_scope is BookingScope.OUTBOUND_ONLY
    assert request.hotel_check_in is None


def test_compile_blocks_conditional_lodging_until_semantics_are_resolved() -> None:
    semantic = _ready_intent(
        lodging_requirement=LodgingRequirement.UNSPECIFIED,
        hotel_check_in=date(2026, 8, 5),
        hotel_check_out=date(2026, 8, 6),
        conditions=["如果当天回不来就住一晚"],
        uncertainties=["是否需要住宿取决于返程库存"],
    )

    compiled = compile_search_command(
        _decision(semantic),
        task_id="conditional-hotel",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert not compiled.ready
    assert compiled.command is None
    assert compiled.clarification_question
    assert any("unresolved" in conflict for conflict in compiled.conflicts)


def test_semantic_entrypoint_clarifies_alternative_origin_without_searching() -> None:
    message = "北京或者上海出发，8月5日去费城"
    semantic = _ready_intent(
        summary="从北京或上海出发去费城，出发地待确认",
        origin_candidates=["Beijing", "Shanghai"],
        destination_candidates=["Philadelphia"],
        alternatives=["北京或者上海出发"],
        uncertainties=["出发城市尚未确定"],
    )
    model = ScriptedSemanticModel(
        [
            _decision(
                semantic,
                status=IntentDecisionStatus.NEEDS_CLARIFICATION,
                clarification_question="请确认从北京还是上海出发？",
                evidence=[
                    EvidenceRef(turn_index=0, field="origin", quote="北京或者上海"),
                    EvidenceRef(turn_index=0, field="destination", quote="费城"),
                ],
            )
        ]
    )
    workflow, _ = build_demo_system(
        semantic_language_model=model,
        clock=lambda: datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    task = workflow.create_task_from_semantic_message(message, traveler_id="E1001")

    assert task.state is TaskState.NEEDS_CLARIFICATION
    assert task.request is None
    assert task.clarification_question == "请确认从北京还是上海出发？"
    assert model.extract_calls == 0

    with pytest.raises(WorkflowError, match="semantic intent entrypoint"):
        workflow.submit_message(task.task_id, "北京")


def test_semantic_revision_reinterprets_full_ledger_and_drops_stale_route_time() -> None:
    first_message = "8月5日从北京去上海，8月6日上午10点前到，不住酒店"
    revision = "改从北京去伦敦，8月10日出发，8月11日上午10点前到"
    first = _decision(
        _ready_intent(),
        evidence=[
            EvidenceRef(turn_index=0, field="origin", quote="北京"),
            EvidenceRef(turn_index=0, field="destination", quote="上海"),
            EvidenceRef(turn_index=0, field="departure_after", quote="8月5日"),
            EvidenceRef(turn_index=0, field="arrive_by", quote="8月6日上午10点"),
        ],
    )
    revised = _decision(
        _ready_intent(
            summary="从北京到伦敦，8月10日出发，8月11日上午10点前到",
            destination_candidates=["London"],
            departure_after=datetime(2026, 8, 10, 5, 0, tzinfo=SHANGHAI),
            arrive_by=datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI),
        ),
        evidence=[
            EvidenceRef(turn_index=1, field="origin", quote="北京"),
            EvidenceRef(turn_index=1, field="destination", quote="伦敦"),
            EvidenceRef(turn_index=1, field="departure_after", quote="8月10日"),
            EvidenceRef(turn_index=1, field="arrive_by", quote="8月11日上午10点"),
        ],
    )
    model = ScriptedSemanticModel([first, revised])
    workflow, _ = build_demo_system(
        semantic_language_model=model,
        clock=lambda: datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    task = workflow.create_task_from_semantic_message(first_message, traveler_id="E1001")
    assert task.state is TaskState.WAITING_FOR_USER

    task = workflow.submit_semantic_message(task.task_id, revision)

    assert task.request is not None
    assert task.request.destination == "London"
    assert task.request.departure_after.day == 10
    assert "[turn:0 role:user]" in model.conversations[-1]
    assert "[turn:1 role:user]" in model.conversations[-1]
    assert revision in model.conversations[-1]
    assert model.extract_calls == 0


def test_openai_adapter_uses_semantic_schema_and_complete_ledger() -> None:
    message = "[turn:0 role:user] 从北京去上海"
    expected = _decision(
        _ready_intent(),
        evidence=[
            EvidenceRef(turn_index=0, field="origin", quote="北京"),
            EvidenceRef(turn_index=0, field="destination", quote="上海"),
        ],
    )

    class Responses:
        def parse(self, **request: object) -> object:
            assert request["text_format"] is IntentDecision
            assert request["input"][1]["content"] == message  # type: ignore[index]
            return SimpleNamespace(
                output_parsed=expected,
                usage=None,
                model="semantic-test-model",
                id="semantic-response",
                service_tier=None,
            )

    adapter = OpenAISemanticIntentLanguageModel(
        client=SimpleNamespace(responses=Responses()),
        model="semantic-test-model",
    )
    legacy_adapter = OpenAIResponsesLanguageModel(
        client=SimpleNamespace(responses=Responses()),
        model="semantic-test-model",
    )
    assert not hasattr(adapter, "extract_trip_intent")
    assert not hasattr(legacy_adapter, "interpret_trip_intent")

    result = adapter.interpret_trip_intent(
        message,
        task_id="semantic-adapter",
        traveler_id="E1001",
        context={
            "reference_time": "2026-08-01T09:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )

    assert result.decision == expected
    assert result.metadata.prompt_version == "semantic-trip-intent-v1"
    assert result.metadata.evidence_contract_version == "conversation-turn-v1"


def test_legacy_entrypoint_stays_separate_from_semantic_flow() -> None:
    class LegacyModel:
        prompt_version = "legacy-scripted-v1"

        def extract_trip_intent(self, *_: object, **__: object) -> IntentExtractionResult:
            payload = IntentExtractionSchema(
                classification="TRIP",
                fields=TripIntentFields(
                    origin="Beijing",
                    destination="Shanghai",
                    departure_after=datetime(2026, 8, 5, 5, 0, tzinfo=SHANGHAI),
                    arrive_by=datetime(2026, 8, 6, 10, 0, tzinfo=SHANGHAI),
                    return_after=None,
                    return_before=None,
                    hotel_check_in=None,
                    hotel_check_out=None,
                    client_location=None,
                    hard_constraints=["arrive_before_meeting"],
                    soft_preferences=[],
                ),
                provided_fields=[
                    "origin",
                    "destination",
                    "departure_after",
                    "arrive_by",
                    "hard_constraints",
                ],
                missing_required_fields=[],
                conflicts=[],
                assumptions=[],
                unsupported_capabilities=[],
                confidence=0.95,
                manipulation_detected=False,
            )
            return IntentExtractionResult(
                payload=payload,
                metadata=LLMCallMetadata(
                    prompt_version=self.prompt_version,
                    model="legacy-scripted",
                    duration_ms=1,
                ),
            )

        def propose_search_adjustment(self, *_: object) -> None:
            return None

        def explain_verified_options(self, *_: object) -> dict[str, str]:
            return {}

    workflow, _ = build_demo_system(
        language_model=LegacyModel(),
        clock=lambda: datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    task = workflow.create_task_from_message(
        "8月5日从北京去上海，8月6日上午10点前到",
        traveler_id="E1001",
    )

    assert task.state is TaskState.WAITING_FOR_USER
    assert task.metadata["intent_entrypoint"] == "legacy"
    assert "semantic_intent" not in task.metadata

    with pytest.raises(WorkflowError, match="legacy intent entrypoint"):
        workflow.submit_semantic_message(task.task_id, "改去伦敦")
