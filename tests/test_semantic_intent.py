from __future__ import annotations

import json
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
    LanguageModelError,
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
        _decision(
            _ready_intent(),
            evidence=[
                EvidenceRef(turn_index=0, field=field, quote="北京")
                for field in ("origin", "destination", "departure_after", "arrive_by")
            ],
        ),
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
    assert result.metadata.prompt_version == "semantic-trip-intent-v6"
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


def test_chat_mode_prompt_spells_out_the_envelope_not_just_a_json_schema() -> None:
    """没有严格 schema 强制的接口容易把顶层字段塞进 intent 里；提示词必须明确禁止。

    这是真实跑 DeepSeek 时遇到的问题：模型把 evidence / confidence /
    manipulation_detected 等放进了 intent 对象内部，整份解释因此作废。
    """
    captured: dict[str, object] = {}

    class Completions:
        def create(self, **request: object) -> object:
            captured.update(request)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=_decision(_ready_intent()).model_dump_json()
                        )
                    )
                ],
                usage=None,
                model="deepseek-test",
                id="chat-response",
            )

    adapter = OpenAISemanticIntentLanguageModel(
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        model="deepseek-test",
        api_mode="chat",
    )

    adapter.interpret_trip_intent(
        "[turn:0 role:user] 从北京去上海",
        task_id="chat-adapter",
        traveler_id="E1001",
        context={
            "reference_time": "2026-08-01T09:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )

    prompt = captured["messages"][0]["content"]  # type: ignore[index]
    assert "top-level keys are exactly" in prompt
    assert "must never appear inside it" in prompt
    for key in ("status", "evidence", "confidence", "manipulation_detected"):
        assert key in prompt
    # intent 自己的字段也要列全，模型才知道边界在哪。
    for key in ("origin_candidates", "uncertainties", "conditions"):
        assert key in prompt


def test_prompt_tells_the_model_which_evidence_a_ready_decision_must_carry() -> None:
    """宿主对 READY 强制要求四项证据；提示词必须把这条规则说给模型听。

    这是真实多轮对话里遇到的问题：用户只在最后一句补了返程时间，模型就只给
    返程的证据，出发地/目的地的引用丢了，整份"可以查了"的解释因此作废。
    """
    prompt = OpenAISemanticIntentLanguageModel._semantic_system_prompt(
        {"reference_time": "2026-08-01T09:00:00+08:00", "timezone": "Asia/Shanghai"}
    )

    assert "When status is READY" in prompt
    assert "origin, destination, departure_after and arrive_by" in prompt
    # 必须点明可以引用更早的轮次，否则模型只会盯着最新一句。
    assert "including" in prompt and "earlier turns" in prompt


def test_chat_mode_lifts_decision_keys_that_the_model_nested_inside_intent() -> None:
    """模型把顶层字段塞进 intent 时，只把它们搬回顶层，不改任何取值。

    真实跑 DeepSeek 时 8 次里有 1 次这样。提示词已经写清了信封结构，但没有严格
    schema 强制的接口不保证遵从，所以这里做一次有界的结构修复兜底。
    """
    decision = _decision(_ready_intent())
    payload = decision.model_dump(mode="json")
    # 造出模型犯的那个错：把 evidence / confidence 等塞进 intent 里面。
    misplaced = {"evidence", "confidence", "manipulation_detected", "conflicts"}
    broken = {
        **{key: value for key, value in payload.items() if key not in misplaced | {"intent"}},
        "intent": {**payload["intent"], **{key: payload[key] for key in misplaced}},
    }

    class Completions:
        def create(self, **request: object) -> object:
            del request
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(broken, ensure_ascii=False))
                    )
                ],
                usage=None,
                model="deepseek-test",
                id="chat-response",
            )

    adapter = OpenAISemanticIntentLanguageModel(
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        model="deepseek-test",
        api_mode="chat",
    )

    result = adapter.interpret_trip_intent(
        "[turn:0 role:user] 从北京去上海",
        task_id="repair",
        traveler_id="E1001",
        context={"reference_time": "2026-08-01T09:00:00+08:00", "timezone": "Asia/Shanghai"},
    )

    assert result.decision == decision
    assert result.metadata.envelope_repaired is True


def test_unrepairable_json_is_retryable_rather_than_a_permanent_failure() -> None:
    """修不好就当作偶发的模型格式故障，让上层重试一次，而不是直接判死。"""

    class Completions:
        def create(self, **request: object) -> object:
            del request
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"status": "READY"}'))
                ],
                usage=None,
                model="deepseek-test",
                id="chat-response",
            )

    adapter = OpenAISemanticIntentLanguageModel(
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        model="deepseek-test",
        api_mode="chat",
    )

    with pytest.raises(LanguageModelError) as excinfo:
        adapter.interpret_trip_intent(
            "[turn:0 role:user] 从北京去上海",
            task_id="broken",
            traveler_id="E1001",
            context={"reference_time": "2026-08-01T09:00:00+08:00", "timezone": "Asia/Shanghai"},
        )

    assert excinfo.value.error_code == "SEMANTIC_JSON_INVALID"
    assert excinfo.value.retryable is True


def test_prompt_defaults_a_yearless_date_to_this_year_and_asks_when_it_has_passed() -> None:
    prompt = OpenAISemanticIntentLanguageModel._semantic_system_prompt(
        {"reference_time": "2026-08-19T15:00:00+08:00", "timezone": "Asia/Shanghai"}
    )

    assert "bare month/day with no year" in prompt
    assert "current year" in prompt
    assert "next year" in prompt
    # 默认分支必须写得比例外分支更响，否则模型会对每个日期都问一遍年份。
    assert "Do this silently" in prompt
    assert "must not ask" in prompt
    # "别问"只针对年份，不能连该问的到达时限也一起压掉。
    assert "about the year only" in prompt
    assert "exactly one situation" in prompt
    assert "strictly earlier than the reference_time day" in prompt
    # 两个方向都要给例子：该问的和不该问的。
    assert "still ahead, so resolve it and ask nothing" in prompt
    assert "already behind, so ask" in prompt
    # 写了年份的日期即使在过去也不算歧义，否则 2026.8.5 会被误拦。
    assert "stated explicitly is never ambiguous" in prompt


def test_a_ready_decision_without_grounded_evidence_asks_instead_of_dead_ending() -> None:
    """模型说"可以查了"却拿不出某字段的原话，是"还没问清"，不是把整份解释判死。

    真实跑里最常见的情形：用户压根没说到达时限。此前宿主把它当模型违约，直接把用户
    推去填结构化表单；现在改成追问缺的那一项，已经读懂的日期照样保留。
    """
    compiled = compile_search_command(
        _decision(
            _ready_intent(),
            evidence=[
                EvidenceRef(turn_index=0, field="origin", quote="北京"),
                EvidenceRef(turn_index=0, field="destination", quote="上海"),
                EvidenceRef(turn_index=0, field="departure_after", quote="8月5日"),
            ],
        ),
        task_id="ungrounded-arrive",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(),
        created_at=datetime(2026, 8, 1, 9, 0, tzinfo=SHANGHAI),
    )

    assert not compiled.ready
    assert compiled.command is None
    assert compiled.missing == ("arrive_by",)
    assert compiled.clarification_question


def test_a_fabricated_quote_is_still_a_hard_model_failure() -> None:
    """引文在原文里找不到仍然是硬违约——放松的只是"少给一条证据"。"""
    from corporate_travel_agent.agent.semantic_intent import ConversationIntentInterpreter

    ledger = ConversationLedger.from_messages(
        [ConversationMessage(role="user", content="8月5日从北京去上海")]
    )
    model = ScriptedSemanticModel(
        [
            _decision(
                _ready_intent(),
                evidence=[EvidenceRef(turn_index=0, field="origin", quote="用户没说过这句")],
            )
        ]
    )

    with pytest.raises(LanguageModelError) as excinfo:
        ConversationIntentInterpreter(model).interpret(
            ledger, task_id="fabricated", traveler_id="E1001", context={}
        )

    assert excinfo.value.error_code == "INTENT_EVIDENCE_QUOTE_INVALID"
