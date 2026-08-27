"""Single-owner semantic interpretation for a complete travel conversation.

This module owns what the traveler means.  It deliberately keeps ambiguity,
alternatives and conditions until an executable command can be compiled safely.
Callers must not patch individual semantic fields after an interpretation.
"""

from __future__ import annotations

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
    grounded = {item.field for item in decision.evidence}
    return tuple(field for field in REQUIRED_EVIDENCE_FIELDS if field not in grounded)
