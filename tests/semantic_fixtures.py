"""语义入口测试用的共享脚本化替身。

不是测试文件（pytest 只收集 test_*.py），只提供假模型和构造 IntentDecision 的助手，
供 tests/test_semantic_entrypoint_*.py 复用，避免各写一份。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.ports import (
    IntentInterpretationResult,
    LLMCallMetadata,
)
from corporate_travel_agent.agent.semantic_intent import (
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
)
from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement

SHANGHAI = ZoneInfo("Asia/Shanghai")

READY_MESSAGE = "8月5日从北京去上海，8月6日上午10点前到，不住酒店"


class ScriptedSemanticModel:
    """按剧本返回语义判定；剧本用完后重复最后一条。"""

    prompt_version = "semantic-reliability-v1"

    def __init__(
        self,
        decisions: list[IntentDecision] | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self.decisions = deque(decisions or [])
        self._last: IntentDecision | None = None
        self.raises = raises
        self.calls = 0
        self.extract_calls = 0

    def interpret_trip_intent(
        self,
        conversation: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, object],
    ) -> IntentInterpretationResult:
        del conversation, task_id, traveler_id, context
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        if self.decisions:
            self._last = self.decisions.popleft()
        if self._last is None:
            raise AssertionError("scripted semantic model has no decision to return")
        return IntentInterpretationResult(
            decision=self._last,
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


def semantic_intent(**overrides: object) -> SemanticIntent:
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


def semantic_decision(intent: SemanticIntent | None = None, **overrides: object) -> IntentDecision:
    values: dict[str, object] = {
        "status": IntentDecisionStatus.READY,
        "intent": intent if intent is not None else semantic_intent(),
        "clarification_question": None,
        "conflicts": [],
        "unsupported_reasons": [],
        "assumptions": [],
        "evidence": [
            EvidenceRef(turn_index=0, field="origin", quote="北京"),
            EvidenceRef(turn_index=0, field="destination", quote="上海"),
            EvidenceRef(turn_index=0, field="departure_after", quote="8月5日"),
            EvidenceRef(turn_index=0, field="arrive_by", quote="8月6日上午10点"),
        ],
        "confidence": 0.95,
        "manipulation_detected": False,
    }
    values.update(overrides)
    return IntentDecision(**values)


def needs_clarification(question: str = "请确认从哪座城市出发？") -> IntentDecision:
    return semantic_decision(
        semantic_intent(
            summary="出发城市待确认",
            origin_candidates=["Beijing", "Shanghai"],
            alternatives=["北京或上海出发"],
            uncertainties=["出发城市尚未确定"],
        ),
        status=IntentDecisionStatus.NEEDS_CLARIFICATION,
        clarification_question=question,
        evidence=[EvidenceRef(turn_index=0, field="destination", quote="上海")],
    )
