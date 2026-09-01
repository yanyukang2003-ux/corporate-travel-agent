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
    LanguageModelError,
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


class LegScopedRequirement(BaseModel):
    """一条只管某一段的要求或偏好。

    ``leg_index`` 数的是这趟行程的第几段：0 是去程，1 是返程或第二段。
    "去程直飞就行、返程无所谓"整句话的意义就落在这个数字上——没有它，
    宿主只能在"把直飞放大到全程"和"整条丢掉"之间二选一，两个都是错的。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=60)
    leg_index: int = Field(ge=0)


class SemanticLeg(BaseModel):
    """行程里的一段：从哪到哪、什么时候走、什么时候要到。

    **只在三段及以上时才用。** 单程和往返由下面那几个扁平字段表达——它们已经够用，
    而且前端、评测与冻结数据集都认那几个名字。这个数组是为了放下"北京→上海→杭州→
    北京"里中间那一段：扁平字段一共只有两个时间窗，第三段无处可放。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=1, max_length=120)
    depart_after: datetime | None
    arrive_before: datetime | None


class SemanticStay(BaseModel):
    """一次过夜：在哪座城市、住哪几天。

    同样只在**多于一处住宿**时才用。多城行程有 N-1 个过夜点，
    ``hotel_check_in`` / ``hotel_check_out`` 一对日期只放得下一处。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    city: str = Field(min_length=1, max_length=120)
    check_in: date | None
    check_out: date | None


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
    # 上面两串名字里，哪些其实只管某一段。名字仍然要出现在上面的列表里——
    # 这两个数组只是给它加个作用域，不是另起一份清单。
    leg_scoped_hard_constraints: list[LegScopedRequirement] = Field(default_factory=list)
    leg_scoped_soft_preferences: list[LegScopedRequirement] = Field(default_factory=list)
    # 三段及以上的行程走这里；一两段留空，按上面的扁平字段理解。
    # **留空是正常情况**——绝大多数差旅是单程或往返，多写一份只会多一处对不上。
    legs: list[SemanticLeg] = Field(default_factory=list)
    # 多于一处住宿时走这里；一处或不住留空。
    stays: list[SemanticStay] = Field(default_factory=list)
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


