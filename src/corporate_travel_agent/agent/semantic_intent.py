"""Single-owner semantic interpretation for a complete travel conversation.

This module owns what the traveler means.  It deliberately keeps ambiguity,
alternatives and conditions until an executable command can be compiled safely.
Callers must not patch individual semantic fields after an interpretation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement
from corporate_travel_agent.domain.models import ConversationMessage

from .ports import (
    IntentInterpretationResult,
    LanguageModelError,
    SemanticLanguageModelPort,
)


class IntentDecisionStatus(StrEnum):
    """The only host-visible outcomes of semantic interpretation."""

    READY = "READY"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    UNSUPPORTED = "UNSUPPORTED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class EvidenceRef(BaseModel):
    """A claim-to-conversation reference; quotes must occur in the named turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_index: int = Field(ge=0)
    field: str = Field(min_length=1, max_length=80)
    quote: str = Field(min_length=1, max_length=300)


class SemanticIntent(BaseModel):
    """Expressive travel meaning, intentionally distinct from provider parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=1000)
    origin_candidates: list[str]
    destination_candidates: list[str]
    # 旅行者说返程从哪座城市起飞。为空表示他没提，按"从目的地原路返回"理解。
    # 一旦他说了一个别的城市（去 上海、从 杭州 回），这就是开口程——系统只能表达
    # 单程和往返两种形态，所以必须由宿主拒绝，而不是把这座城市悄悄丢掉。
    return_origin_candidates: list[str] = Field(default_factory=list)
    departure_after: datetime | None
    arrive_by: datetime | None
    return_after: datetime | None
    return_before: datetime | None
    booking_scope: BookingScope
    lodging_requirement: LodgingRequirement
    hotel_check_in: date | None
    hotel_check_out: date | None
    client_location: str | None
    hard_constraints: list[str]
    soft_preferences: list[str]
    alternatives: list[str]
    conditions: list[str]
    uncertainties: list[str]


class IntentDecision(BaseModel):
    """Complete result returned through the intent interpreter seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: IntentDecisionStatus
    intent: SemanticIntent
    clarification_question: str | None
    conflicts: list[str]
    unsupported_reasons: list[str]
    assumptions: list[str]
    evidence: list[EvidenceRef]
    confidence: float = Field(ge=0.0, le=1.0)
    manipulation_detected: bool


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """An immutable, stably indexed view of one persisted conversation message."""

    turn_index: int
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationLedger:
    """Append-only conversation view used as the interpreter's source of truth."""

    turns: tuple[ConversationTurn, ...]

    @classmethod
    def from_messages(cls, messages: list[ConversationMessage]) -> ConversationLedger:
        turns: list[ConversationTurn] = []
        for index, message in enumerate(messages):
            if message.role not in {"user", "assistant", "system"}:
                raise ValueError(f"Unsupported conversation role: {message.role!r}")
            content = message.content.strip()
            if not content:
                raise ValueError(f"Conversation turn {index} is empty")
            turns.append(
                ConversationTurn(
                    turn_index=index,
                    role=message.role,
                    content=content,
                    created_at=message.created_at,
                )
            )
        if not any(turn.role == "user" for turn in turns):
            raise ValueError("Conversation ledger must contain a user turn")
        return cls(tuple(turns))

    @property
    def latest_user_message(self) -> str:
        return next(turn.content for turn in reversed(self.turns) if turn.role == "user")

    def render(self) -> str:
        """Render stable turn identifiers without paraphrasing the conversation."""
        return "\n".join(
            f"[turn:{turn.turn_index} role:{turn.role}] {turn.content}" for turn in self.turns
        )

    def as_prompt_messages(self) -> list[dict[str, str]]:
        return [{"role": turn.role, "content": turn.content} for turn in self.turns]


class ConversationIntentInterpreter:
    """Deep module: one complete conversation in, one semantic decision out."""

    def __init__(self, language_model: SemanticLanguageModelPort) -> None:
        self._language_model = language_model

    def interpret(
        self,
        ledger: ConversationLedger,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, Any],
    ) -> IntentInterpretationResult:
        semantic_context = {
            **context,
            "conversation_ledger": ledger.render(),
            "conversation_turns": ledger.as_prompt_messages(),
        }
        result = self._language_model.interpret_trip_intent(
            ledger.render(),
            task_id=task_id,
            traveler_id=traveler_id,
            context=semantic_context,
        )
        _require_grounded_evidence(result.decision, ledger)
        return result


def semantic_fields(decision: IntentDecision) -> dict[str, Any]:
    """Single compatibility projection from semantic state to the legacy task view."""
    intent = decision.intent
    hard = list(intent.hard_constraints)
    if intent.lodging_requirement is LodgingRequirement.REQUIRED:
        hard = list(dict.fromkeys([*hard, "hotel_required"]))
    elif intent.lodging_requirement is LodgingRequirement.NOT_REQUIRED:
        hard = [item for item in hard if item != "hotel_required"]
    return {
        "origin": intent.origin_candidates[0] if len(intent.origin_candidates) == 1 else None,
        "destination": (
            intent.destination_candidates[0]
            if len(intent.destination_candidates) == 1
            else None
        ),
        "departure_after": intent.departure_after,
        "arrive_by": intent.arrive_by,
        "return_after": intent.return_after,
        "return_before": intent.return_before,
        "hotel_check_in": intent.hotel_check_in,
        "hotel_check_out": intent.hotel_check_out,
        "client_location": intent.client_location,
        "booking_scope": intent.booking_scope.value,
        "lodging_requirement": intent.lodging_requirement.value,
        "hard_constraints": hard,
        "soft_preferences": list(intent.soft_preferences),
    }


def _require_grounded_evidence(
    decision: IntentDecision, ledger: ConversationLedger
) -> None:
    for evidence in decision.evidence:
        if evidence.turn_index >= len(ledger.turns):
            raise LanguageModelError(
                "Semantic intent evidence references an unknown conversation turn",
                error_code="INTENT_EVIDENCE_TURN_INVALID",
                layer="semantic_intent",
            )
        turn = ledger.turns[evidence.turn_index]
        if turn.role != "user":
            raise LanguageModelError(
                "Semantic intent evidence must reference a user turn",
                error_code="INTENT_EVIDENCE_ROLE_INVALID",
                layer="semantic_intent",
            )
        if evidence.quote not in turn.content:
            raise LanguageModelError(
                "Semantic intent evidence quote is not grounded in the referenced turn",
                error_code="INTENT_EVIDENCE_QUOTE_INVALID",
                layer="semantic_intent",
            )


REQUIRED_EVIDENCE_FIELDS = ("origin", "destination", "departure_after", "arrive_by")

# 证据里的字段名同时接受 schema 的真名。宿主原本只认 "origin"/"destination"，可这两个
# 名字在 SemanticIntent 里**根本不存在**——真名是 origin_candidates/destination_candidates。
# 模型照 schema 写是完全合理的，却会被判成"没给证据"，于是宿主回头去问用户早就说清的事。
# 要求一个不存在的名字是宿主的坑，不是模型的错，所以两种写法都认。
_EVIDENCE_FIELD_ALIASES = {
    "origin_candidates": "origin",
    "destination_candidates": "destination",
    "return_origin_candidates": "return_origin",
}


def _grounded_fields(decision: IntentDecision) -> set[str]:
    return {
        _EVIDENCE_FIELD_ALIASES.get(item.field, item.field) for item in decision.evidence
    }


def ungrounded_required_fields(decision: IntentDecision) -> tuple[str, ...]:
    """READY 判定里没有原话支撑的必填字段。

    模型说"可以查了"，但拿不出用户在哪句话里说了到达时限，这不是模型违约，
    而是这件事**还没问清**——最常见的情形就是用户压根没提到达时限。
    因此这里返回缺口让编译阶段去追问，而不是把整份解释判死。

    引用了不存在的轮次、引用了助手发言、或者引文在原文里找不到，仍然是硬违约，
    由 `_require_grounded_evidence` 直接抛错。
    """
    if decision.status is not IntentDecisionStatus.READY:
        return ()
    grounded = _grounded_fields(decision)
    return tuple(field for field in REQUIRED_EVIDENCE_FIELDS if field not in grounded)


def _grounding_value(intent: SemanticIntent, field: str) -> Any:
    """取某个必填字段在这份理解里的取值，用来判断它有没有变过。"""
    if field == "origin":
        return tuple(intent.origin_candidates)
    if field == "destination":
        return tuple(intent.destination_candidates)
    return getattr(intent, field, None)


def carried_grounding(
    history: Iterable[Mapping[str, Any]], intent: SemanticIntent
) -> frozenset[str]:
    """先前轮次已经落实、且取值至今没变的必填字段。

    证据规则防的是"模型编了一个用户没说过的城市或日期"。但对话一长，出发地和目的地
    往往是第 0、1 轮说的，模型到第 5 轮只会引用最新那句——于是宿主会回头去问一件用户
    早就说清、系统也早就读对的事。

    账本是累积的，证据也应当累积：某个字段只要**在本任务的某一轮被原话落实过**，
    **并且取值至今没变**，就仍然算落实。取值一旦变了，就必须重新拿出原话。
    """
    carried: set[str] = set()
    for record in history:
        payload = record.get("decision") if isinstance(record, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        try:
            past = IntentDecision.model_validate(payload)
        except Exception:  # noqa: BLE001 - 历史留痕坏了不该阻断当前这轮
            continue
        grounded = _grounded_fields(past)
        for field in REQUIRED_EVIDENCE_FIELDS:
            if field not in grounded:
                continue
            if _grounding_value(past.intent, field) == _grounding_value(intent, field):
                carried.add(field)
    return frozenset(carried)
