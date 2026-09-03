"""订好之后的变更意图：把"会议改到周五了" / "这趟不去了"读成一条可执行的变更请求。

## 模型在这里能做什么

只有两个工具，都是终局：`request_trip_change`（读出来了）和 `ask_traveler`（读不出来）。
没有搜索、没有规划——这趟已经订了，改期任务开出来之后照常走规划链路，那是另一段循环。

## 关卡在工具签名上

- 会议改期必须带 `new_arrive_by`，而且 `date_evidence` 要逐字抄旅行者原话、那句话里得
  真的定下这一天（和 `search_transport` 的日期出处关卡同一条规则 `_quote_fixes_date`）。
  "改到下周"里没有一天，模型不能替他挑一个。
- 航变自述必须指到已订行程里的某张票。
- 取消只要理由。

关卡打回去的原因原样喂回模型，最多两轮；两轮都没读出来就问一句，任务状态不变。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from corporate_travel_agent.agent.tool_loop import (
    _ISO_DATETIME,
    ASK_TRAVELER,
    ModelTurn,
    ToolExchange,
    ToolInputError,
    ToolSpec,
    _optional_datetime,
    _quote_fixes_date,
    _require_text,
    _searchable,
)
from corporate_travel_agent.domain.models import TripLeg, TripTask, TripWatch

CHANGE_INTENT_PROMPT_VERSION = "trip-change-v1"
MAX_ROUNDS = 2


class ChangeKind(StrEnum):
    MEETING_MOVED = "MEETING_MOVED"
    FLIGHT_CHANGED = "FLIGHT_CHANGED"
    CANCEL_TRIP = "CANCEL_TRIP"


REQUEST_TRIP_CHANGE = ToolSpec(
    name="request_trip_change",
    description=(
        "The trip is ALREADY BOOKED. Read the traveler's latest message as ONE change request "
        "and hand it to the host; never search or plan here. kinds: MEETING_MOVED — the "
        "meeting/appointment moved, give `leg_index` (which booked leg must now arrive by the "
        "new time; 0 is the first leg) and `new_arrive_by`, and COPY VERBATIM into "
        "`date_evidence` the traveler's words that fix the new date — if their words do not "
        "name a day, you do not know it: use ask_traveler. FLIGHT_CHANGED — the traveler says "
        "a booked flight was cancelled or moved by the carrier; give the `ref_id` of that leg "
        "from the booked itinerary. CANCEL_TRIP — the traveler is not going at all. "
        "`reason` is the traveler's own explanation, one sentence."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [item.value for item in ChangeKind]},
            "leg_index": {
                "type": "integer",
                "minimum": 0,
                "description": "MEETING_MOVED：改的是已订行程的第几段（0 起）",
            },
            "new_arrive_by": _ISO_DATETIME
            | {"description": "MEETING_MOVED：新的最晚到达时刻"},
            "date_evidence": {
                "type": "string",
                "description": "MEETING_MOVED：定下新日期的旅行者原话，必须逐字抄自对话",
            },
            "ref_id": {
                "type": "string",
                "description": "FLIGHT_CHANGED：受影响那段的票号，必须来自已订行程",
            },
            "reason": {"type": "string", "description": "旅行者自己说的原因，一句话"},
        },
        "required": ["kind", "reason"],
        "additionalProperties": False,
    },
    terminal=True,
)

CHANGE_TOOLS: tuple[ToolSpec, ...] = (REQUEST_TRIP_CHANGE, ASK_TRAVELER)


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    kind: ChangeKind
    reason: str
    leg_index: int | None = None
    new_arrive_by: datetime | None = None
    date_evidence: str | None = None
    ref_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeIntentOutcome:
    """读出来了就是 `change`；读不出来就是 `question`。"""

    change: ChangeRequest | None = None
    question: str | None = None
    transcript: tuple[ToolExchange, ...] = ()


def render_change_conversation(
    *, task: TripTask, watch: TripWatch, journey: Sequence[TripLeg], message: str
) -> str:
    """给模型看的原文：已订行程（带票号、段号、到场时限）、此前对话、旅行者现在说的话。"""
    lines = [f"【已订行程】任务 {task.task_id}，订单号已回填。"]
    for index, leg in enumerate(watch.legs):
        spec = journey[index] if index < len(journey) else None
        deadline = (
            f"，到场时限 {spec.arrive_before.strftime('%Y-%m-%d %H:%M%z')}"
            if spec is not None and spec.arrive_before is not None
            else ""
        )
        lines.append(
            f"第 {index} 段 ref_id={leg.ref_id} {leg.origin}→{leg.destination} "
            f"{leg.depart_at.strftime('%Y-%m-%d %H:%M')} → "
            f"{leg.arrive_at.strftime('%H:%M')}{deadline}"
        )
    earlier = [item for item in task.messages if item.content.strip() != message.strip()]
    if earlier:
        lines.append("【此前对话】")
        lines.extend(f"{item.role}: {item.content}" for item in earlier)
    lines.append(f"【旅行者现在说】{message}")
    return "\n".join(lines)


def run_change_intent(
    *,
    model: Any,
    conversation: str,
    context: Mapping[str, Any],
    watched_refs: tuple[str, ...],
    leg_count: int,
    now: datetime,
    timezone_name: str,
    call: Callable[[Callable[[], ModelTurn]], ModelTurn] | None = None,
) -> ChangeIntentOutcome:
    """最多两轮：关卡打回去的原因喂回模型再试一次；还不行就问旅行者。"""
    transcript: list[ToolExchange] = []
    conversation_index = _searchable(conversation)

    def turn() -> ModelTurn:
        next_turn = getattr(model, "next_turn", None)
        if callable(next_turn):
            return next_turn(
                conversation=conversation,
                transcript=tuple(transcript),
                tools=CHANGE_TOOLS,
                context=dict(context),
            )
        invocation = model.next_tool_call(
            conversation=conversation,
            transcript=tuple(transcript),
            tools=CHANGE_TOOLS,
            context=dict(context),
        )
        return ModelTurn(calls=(invocation,))

    for _ in range(MAX_ROUNDS):
        result = call(turn) if call is not None else turn()
        if not result.calls:
            text = (result.message or "").strip()
            return ChangeIntentOutcome(question=text or None, transcript=tuple(transcript))
        for invocation in result.calls:
            if invocation.name == "ask_traveler":
                try:
                    question = _require_text(invocation.arguments, "question")
                except ToolInputError:
                    question = ""
                return ChangeIntentOutcome(question=question or None, transcript=tuple(transcript))
            if invocation.name != "request_trip_change":
                transcript.append(
                    ToolExchange(
                        invocation=invocation,
                        ok=False,
                        result={
                            "error": f"没有名为 {invocation.name} 的工具",
                            "available": [spec.name for spec in CHANGE_TOOLS],
                        },
                    )
                )
                continue
            try:
                change = validate_change_request(
                    invocation.arguments,
                    conversation_index=conversation_index,
                    watched_refs=watched_refs,
                    leg_count=leg_count,
                    now=now,
                    timezone_name=timezone_name,
                )
            except ToolInputError as exc:
                transcript.append(
                    ToolExchange(
                        invocation=invocation,
                        ok=False,
                        result={"error": str(exc), "field": exc.field_name},
                    )
                )
                continue
            return ChangeIntentOutcome(change=change, transcript=tuple(transcript))
    return ChangeIntentOutcome(transcript=tuple(transcript))


def validate_change_request(
    args: Mapping[str, Any],
    *,
    conversation_index: str,
    watched_refs: tuple[str, ...],
    leg_count: int,
    now: datetime,
    timezone_name: str,
) -> ChangeRequest:
    """工具入口的关卡。打回去的原因是给模型看的，写清楚该怎么改。"""
    raw_kind = _require_text(args, "kind")
    try:
        kind = ChangeKind(raw_kind)
    except ValueError as exc:
        raise ToolInputError(
            f"kind 只能是 {', '.join(item.value for item in ChangeKind)}，不是 {raw_kind!r}",
            field_name="kind",
        ) from exc
    reason = _require_text(args, "reason")

    if kind is ChangeKind.CANCEL_TRIP:
        return ChangeRequest(kind=kind, reason=reason)

    if kind is ChangeKind.FLIGHT_CHANGED:
        ref_id = _require_text(args, "ref_id")
        if ref_id not in watched_refs:
            raise ToolInputError(
                f"ref_id {ref_id!r} 不在已订行程里；可选的是 {', '.join(watched_refs)}",
                field_name="ref_id",
            )
        return ChangeRequest(kind=kind, reason=reason, ref_id=ref_id)

    new_arrive_by = _optional_datetime(args, "new_arrive_by", assume_timezone=timezone_name)
    if new_arrive_by is None:
        raise ToolInputError(
            "MEETING_MOVED 需要 new_arrive_by：新的最晚到达时刻，含日期和时区",
            field_name="new_arrive_by",
        )
    if new_arrive_by <= now:
        raise ToolInputError(
            f"new_arrive_by {new_arrive_by.isoformat()} 已经过去了；参照时刻是 {now.isoformat()}",
            field_name="new_arrive_by",
        )
    raw_index = args.get("leg_index", 0)
    try:
        leg_index = int(raw_index)
    except (TypeError, ValueError) as exc:
        raise ToolInputError("leg_index 必须是整数", field_name="leg_index") from exc
    if not 0 <= leg_index < leg_count:
        raise ToolInputError(
            f"leg_index {leg_index} 越界：已订行程只有 {leg_count} 段（0 到 {leg_count - 1}）",
            field_name="leg_index",
        )
    quote = _require_text(args, "date_evidence")
    if _searchable(quote) not in conversation_index:
        raise ToolInputError(
            f"date_evidence 必须逐字抄自对话原文，但对话里没有这句：{quote!r}。"
            "旅行者没说是哪一天，就用 ask_traveler 去问，别自己挑一个。",
            field_name="date_evidence",
        )
    verdict = _quote_fixes_date(quote, new_arrive_by)
    if verdict is not None:
        raise ToolInputError(verdict, field_name="date_evidence")
    return ChangeRequest(
        kind=kind,
        reason=reason,
        leg_index=leg_index,
        new_arrive_by=new_arrive_by,
        date_evidence=quote,
    )
