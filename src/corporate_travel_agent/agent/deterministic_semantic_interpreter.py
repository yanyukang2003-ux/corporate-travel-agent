"""确定性语义解释器：评测 harness 用的离线 ``SemanticLanguageModelPort`` 替身。

它存在的唯一理由是让**新语义入口**可以在不计费、不联网的前提下逐条跑冻结评测集，
并与旧链路的确定性基线做同输入并排对比。

对齐原则（与 ADR-0002 的 removal gate 对应）：

1. 解析能力刻意与 ``DeterministicChineseIntentParser`` 对齐——中文城市表、中文日期
   规则完全复用旧基线的静态方法。两条链路拿到的"模型能力"因此相同，观测到的差异
   只能来自架构（补丁式槽位 vs 单一属主语义），而不是来自解析器强弱。
2. 额外只认识 D1 工作流用例的**冻结英文模板**语法。该模板由
   ``render_workflow_case_message()`` 生成，是评测集自己的产物，不是自然语言能力。
3. 绝不猜测：拿不到直接引语支撑的字段一律留空，由
   ``compile_search_command()`` 判定澄清。本模块不产生默认值、不做日期兜底。

每个被填上的字段都必须附带一条 ``EvidenceRef``，其 ``quote`` 必须逐字出现在被引用的
用户轮次里；这正是 ``_require_grounded_evidence()`` 会强制校验的契约。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import monotonic
from typing import Any

from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement

from .deterministic_parser import DeterministicChineseIntentParser
from .ports import IntentInterpretationResult, LLMCallMetadata
from .semantic_intent import (
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
)

SEMANTIC_PROMPT_VERSION = "deterministic-semantic-v1"

# D1 冻结模板语法。只在评测集自己的渲染结果上成立，不当作自然语言理解能力。
# 目的地锚定在模板的下一句上，否则 "St. Louis" 这类含点号的城市名会被截断。
_TEMPLATE_ROUTE = re.compile(
    r"trip from (?P<origin>.+?) to (?P<destination>.+?)\. I cannot leave before"
)
_TEMPLATE_DEPART = re.compile(r"I cannot leave before (?P<value>\S+?)(?=[ .])")
_TEMPLATE_ARRIVE = re.compile(r"I must land by (?P<value>\S+?)(?=[ .])")
_TEMPLATE_RETURN_AFTER = re.compile(r"no earlier than (?P<value>\S+?)(?=[ .])")
_TEMPLATE_RETURN_BEFORE = re.compile(r"back by (?P<value>\S+?)(?=[ .])")
_TEMPLATE_HOTEL = re.compile(
    r"The hotel runs (?P<check_in>\d{4}-\d{2}-\d{2}) to (?P<check_out>\d{4}-\d{2}-\d{2})\."
)

# 中文直述路线。与旧基线的 ``_cities`` 并存：先用显式路线，再退回城市顺序推断。
_ROUTE_ZH = re.compile(r"从(?P<origin>[一-鿿]{2,8}?)(?:出发)?[到去往](?P<destination>[一-鿿]{2,8})")

# D1 模板里的硬约束/软偏好短语 → 领域 code。首字母可能被模板大写。
_CONSTRAINT_PHRASES: dict[str, str] = {
    "i have to be there before my meeting starts": "arrive_before_meeting",
    "i also need a hotel booked for the nights in between": "hotel_required",
    "the trip has to be a direct connection": "direct_only",
    "please keep me on trains only": "train_only",
    "please keep me on flights only": "flight_only",
}
_PREFERENCE_PHRASES: dict[str, str] = {
    "keep the total as cheap as you can": "lowest_cost",
    "keep the travel time as short as you can": "shortest_duration",
    "avoid very early departures if possible": "avoid_early_departure",
    "a hotel close to the client office would be better": "hotel_near_client",
    "i would rather take the train": "prefer_train",
    "i would rather fly": "prefer_flight",
    "show me both train and flight options": "compare_train_and_flight",
}


@dataclass(frozen=True, slots=True)
class _Grounded:
    """一个有直接引语支撑的取值。"""

    value: Any
    turn_index: int
    quote: str


class DeterministicSemanticInterpreter:
    """离线语义解释替身；实现 ``SemanticLanguageModelPort``，不含旧字段抽取能力。"""

    semantic_prompt_version = SEMANTIC_PROMPT_VERSION
    prompt_version = SEMANTIC_PROMPT_VERSION

    def __init__(self, *, model_name: str = "deterministic-semantic-interpreter") -> None:
        self.model_name = model_name
        self.calls = 0

    def interpret_trip_intent(
        self,
        conversation: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, Any],
    ) -> IntentInterpretationResult:
        """把整段对话解释成一个语义决策；歧义与缺失一律保留为澄清。"""
        del task_id, traveler_id
        started = monotonic()
        self.calls += 1
        turns = _user_turns(conversation, context)
        decision = _interpret(turns, context)
        return IntentInterpretationResult(
            decision=decision,
            metadata=LLMCallMetadata(
                prompt_version=self.semantic_prompt_version,
                model=self.model_name,
                duration_ms=int((monotonic() - started) * 1000),
                evidence_contract_version="conversation-turn-v1",
            ),
        )


def _user_turns(conversation: str, context: dict[str, Any]) -> tuple[tuple[int, str], ...]:
    """返回 ``(turn_index, content)``；索引必须与 ledger 的稳定索引一致。"""
    raw = context.get("conversation_turns")
    if isinstance(raw, list) and raw:
        return tuple(
            (index, str(item.get("content", "")))
            for index, item in enumerate(raw)
            if isinstance(item, dict) and item.get("role") == "user"
        )
    # 没有结构化轮次时解析 ledger 的稳定渲染，绝不重新编号。
    turns: list[tuple[int, str]] = []
    for line in conversation.splitlines():
        match = re.match(r"\[turn:(?P<index>\d+) role:(?P<role>\w+)\] (?P<content>.*)", line)
        if match is not None and match.group("role") == "user":
            turns.append((int(match.group("index")), match.group("content")))
    return tuple(turns)


def _interpret(
    turns: tuple[tuple[int, str], ...],
    context: dict[str, Any],
) -> IntentDecision:
    """按整段对话重新推导语义；后出现的表述覆盖先前表述。"""
    if not turns:
        return _clarify(
            summary="对话中没有可解释的用户表述。",
            question="请描述你的出行需求。",
        )

    origin: _Grounded | None = None
    destination: _Grounded | None = None
    departure_after: _Grounded | None = None
    arrive_by: _Grounded | None = None
    return_after: _Grounded | None = None
    return_before: _Grounded | None = None
    hotel_check_in: _Grounded | None = None
    hotel_check_out: _Grounded | None = None
    hard_constraints: list[str] = []
    soft_preferences: list[str] = []
    origin_candidates_multi: tuple[str, ...] = ()
    destination_candidates_multi: tuple[str, ...] = ()
    out_of_scope_quote: tuple[int, str] | None = None

    reference_time = _reference_time(context)

    for turn_index, content in turns:
        route = _route(content, turn_index)
        if route is not None:
            found_origin, found_destination = route
            if found_origin is not None:
                origin = found_origin
                origin_candidates_multi = ()
            if found_destination is not None:
                destination = found_destination
                destination_candidates_multi = ()

        for grounded, target in (
            (_template_instant(content, turn_index, _TEMPLATE_DEPART), "departure_after"),
            (_template_instant(content, turn_index, _TEMPLATE_ARRIVE), "arrive_by"),
            (_template_instant(content, turn_index, _TEMPLATE_RETURN_AFTER), "return_after"),
            (_template_instant(content, turn_index, _TEMPLATE_RETURN_BEFORE), "return_before"),
        ):
            if grounded is None:
                continue
            if target == "departure_after":
                departure_after = grounded
            elif target == "arrive_by":
                arrive_by = grounded
            elif target == "return_after":
                return_after = grounded
            else:
                return_before = grounded

        if departure_after is None:
            zh_departure = _chinese_departure(content, turn_index, reference_time, context)
            if zh_departure is not None:
                departure_after = zh_departure

        hotel = _TEMPLATE_HOTEL.search(content)
        if hotel is not None:
            quote = hotel.group(0)
            hotel_check_in = _Grounded(
                date.fromisoformat(hotel.group("check_in")), turn_index, quote
            )
            hotel_check_out = _Grounded(
                date.fromisoformat(hotel.group("check_out")), turn_index, quote
            )

        for code in _phrase_codes(content, _CONSTRAINT_PHRASES):
            if code not in hard_constraints:
                hard_constraints.append(code)
        for code in _phrase_codes(content, _PREFERENCE_PHRASES):
            if code not in soft_preferences:
                soft_preferences.append(code)
        if DeterministicChineseIntentParser._classification(content) == "TRANSPORT_COMPARE":
            if "compare_train_and_flight" not in soft_preferences:
                soft_preferences.append("compare_train_and_flight")
        if DeterministicChineseIntentParser._classification(content) == "OUT_OF_SCOPE":
            out_of_scope_quote = (turn_index, content)

    evidence: list[EvidenceRef] = []
    for field_name, grounded in (
        ("origin", origin),
        ("destination", destination),
        ("departure_after", departure_after),
        ("arrive_by", arrive_by),
        ("return_after", return_after),
        ("return_before", return_before),
        ("hotel_check_in", hotel_check_in),
        ("hotel_check_out", hotel_check_out),
    ):
        if grounded is not None:
            evidence.append(
                EvidenceRef(
                    turn_index=grounded.turn_index,
                    field=field_name,
                    quote=grounded.quote,
                )
            )

    lodging = (
        LodgingRequirement.REQUIRED
        if "hotel_required" in hard_constraints
        else LodgingRequirement.UNSPECIFIED
    )
    booking_scope = (
        BookingScope.ROUND_TRIP
        if return_after is not None or return_before is not None
        else BookingScope.OUTBOUND_ONLY
    )
    intent = SemanticIntent(
        summary=_summary(origin, destination, departure_after),
        origin_candidates=(
            [origin.value] if origin is not None else list(origin_candidates_multi)
        ),
        destination_candidates=(
            [destination.value]
            if destination is not None
            else list(destination_candidates_multi)
        ),
        departure_after=departure_after.value if departure_after is not None else None,
        arrive_by=arrive_by.value if arrive_by is not None else None,
        return_after=return_after.value if return_after is not None else None,
        return_before=return_before.value if return_before is not None else None,
        booking_scope=booking_scope,
        lodging_requirement=lodging,
        hotel_check_in=hotel_check_in.value if hotel_check_in is not None else None,
        hotel_check_out=hotel_check_out.value if hotel_check_out is not None else None,
        client_location=None,
        hard_constraints=list(hard_constraints),
        soft_preferences=list(soft_preferences),
        alternatives=[],
        conditions=[],
        uncertainties=[],
    )

    if out_of_scope_quote is not None:
        turn_index, content = out_of_scope_quote
        return IntentDecision(
            status=IntentDecisionStatus.OUT_OF_SCOPE,
            intent=intent,
            clarification_question=None,
            conflicts=[],
            unsupported_reasons=["request is outside corporate travel planning scope"],
            assumptions=[],
            evidence=[
                EvidenceRef(turn_index=turn_index, field="scope", quote=content[:300])
            ],
            confidence=1.0,
            manipulation_detected=False,
        )

    missing = [
        name
        for name, grounded in (
            ("origin", origin),
            ("destination", destination),
            ("departure_after", departure_after),
            ("arrive_by", arrive_by),
        )
        if grounded is None
    ]
    if lodging is LodgingRequirement.REQUIRED and hotel_check_in is None:
        missing.append("hotel_check_in")
    if lodging is LodgingRequirement.REQUIRED and hotel_check_out is None:
        missing.append("hotel_check_out")

    if missing:
        return IntentDecision(
            status=IntentDecisionStatus.NEEDS_CLARIFICATION,
            intent=intent,
            clarification_question=(
                "请确认这些出行信息后我再搜索：" + "、".join(missing) + "。"
            ),
            conflicts=[],
            unsupported_reasons=[],
            assumptions=[],
            evidence=evidence,
            confidence=0.4,
            manipulation_detected=False,
        )

    return IntentDecision(
        status=IntentDecisionStatus.READY,
        intent=intent,
        clarification_question=None,
        conflicts=[],
        unsupported_reasons=[],
        assumptions=[],
        evidence=evidence,
        confidence=0.9,
        manipulation_detected=False,
    )


def _clarify(*, summary: str, question: str) -> IntentDecision:
    return IntentDecision(
        status=IntentDecisionStatus.NEEDS_CLARIFICATION,
        intent=SemanticIntent(
            summary=summary,
            origin_candidates=[],
            destination_candidates=[],
            departure_after=None,
            arrive_by=None,
            return_after=None,
            return_before=None,
            booking_scope=BookingScope.OUTBOUND_ONLY,
            lodging_requirement=LodgingRequirement.UNSPECIFIED,
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=[],
            soft_preferences=[],
            alternatives=[],
            conditions=[],
            uncertainties=[],
        ),
        clarification_question=question,
        conflicts=[],
        unsupported_reasons=[],
        assumptions=[],
        evidence=[],
        confidence=0.0,
        manipulation_detected=False,
    )


def _route(content: str, turn_index: int) -> tuple[_Grounded | None, _Grounded | None] | None:
    """按「显式路线 → 中文路线 → 旧基线城市顺序」的次序推导，不做反向猜测。"""
    template = _TEMPLATE_ROUTE.search(content)
    if template is not None:
        quote = template.group(0)
        return (
            _Grounded(template.group("origin").strip(), turn_index, quote),
            _Grounded(template.group("destination").strip(), turn_index, quote),
        )
    chinese = _ROUTE_ZH.search(content)
    if chinese is not None:
        quote = chinese.group(0)
        return (
            _Grounded(chinese.group("origin"), turn_index, quote),
            _Grounded(chinese.group("destination"), turn_index, quote),
        )
    legacy_origin, legacy_destination = DeterministicChineseIntentParser._cities(content)
    if legacy_origin is None and legacy_destination is None:
        return None
    return (
        _Grounded(legacy_origin, turn_index, legacy_origin) if legacy_origin else None,
        (
            _Grounded(legacy_destination, turn_index, legacy_destination)
            if legacy_destination
            else None
        ),
    )


def _template_instant(
    content: str,
    turn_index: int,
    pattern: re.Pattern[str],
) -> _Grounded | None:
    match = pattern.search(content)
    if match is None:
        return None
    raw = match.group("value")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return _Grounded(value, turn_index, match.group(0))


def _chinese_departure(
    content: str,
    turn_index: int,
    reference_time: datetime,
    context: dict[str, Any],
) -> _Grounded | None:
    """复用旧基线的中文出发日规则，保证两条链路解析能力一致。"""
    from zoneinfo import ZoneInfo

    timezone = ZoneInfo(str(context.get("timezone", "Asia/Shanghai")))
    reference = (
        reference_time.replace(tzinfo=timezone)
        if reference_time.tzinfo is None
        else reference_time.astimezone(timezone)
    )
    value = DeterministicChineseIntentParser._departure_date(content, reference, timezone)
    if value is None:
        return None
    quote = _departure_quote(content)
    if quote is None:
        return None
    return _Grounded(value, turn_index, quote)


def _departure_quote(content: str) -> str | None:
    """取出支撑出发日的原文片段；找不到逐字片段就放弃填充。"""
    if "明天" in content:
        return "明天"
    match = re.search(r"\d{1,2}月\d{1,2}[号日]?", content)
    return match.group(0) if match is not None else None


def _phrase_codes(content: str, phrases: dict[str, str]) -> tuple[str, ...]:
    lowered = content.casefold()
    return tuple(code for phrase, code in phrases.items() if phrase in lowered)


def _reference_time(context: dict[str, Any]) -> datetime:
    raw = context.get("reference_time")
    if isinstance(raw, str) and raw:
        return datetime.fromisoformat(raw)
    return datetime.now(UTC)


def _summary(
    origin: _Grounded | None,
    destination: _Grounded | None,
    departure_after: _Grounded | None,
) -> str:
    parts = [
        f"出发地={origin.value if origin else '未确定'}",
        f"目的地={destination.value if destination else '未确定'}",
        f"最早出发={departure_after.value.isoformat() if departure_after else '未确定'}",
    ]
    return "确定性语义解释：" + "，".join(parts)
