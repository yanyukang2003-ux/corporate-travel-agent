"""按分歧提问：只问那些**答案会改变结论**的事。

今天的追问依据是「字段填满了没有」——这是填表，不是规划。实测里一段五轮的对话，
系统一轮问一个字段问了五次，其中好几个问题的答案根本不改变最终推荐哪班车。

这里做的是 §30.7 那个循环里最便宜的一段：**先不停下来问**，把每件还没定的事的
几种读法各摊开算一遍**行程轮廓**（走哪几段、什么时候、住不住）。

- 几种读法算出来的轮廓**一模一样** → 这件事不影响结论，采纳其中一种，记成假设，不问；
- 轮廓**不一样** → 这才是真分歧，问它。

这一步不查任何库存、不调任何模型：路线在时间上成不成立、要不要多住一晚，
靠地理和时间就能算，只有最后排序才需要真实报价。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime

from corporate_travel_agent.domain.enums import BookingScope


@dataclass(frozen=True, slots=True)
class SketchInput:
    """算一份行程轮廓所需要的全部输入；未定的字段留 None。"""

    origin: str | None
    destination: str | None
    return_origin: str | None
    departure_after: datetime | None
    arrive_by: datetime | None
    return_after: datetime | None
    return_before: datetime | None
    booking_scope: BookingScope
    lodging_required: bool
    hotel_check_in: date | None
    hotel_check_out: date | None
    requirements: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PlanSketch:
    """不查库存就能算出来的行程轮廓。

    只放**会改变推荐结果**的东西：走哪几段、每段的时间窗、住不住、受哪些要求管。
    具体选哪一班、多少钱都不在这里——那要等报价，而报价要花钱。
    """

    legs: tuple[tuple[str, str, str, str], ...]
    lodging: tuple[str, str] | None
    requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    """一件还没定下来的事，以及它有哪几种读法。

    ``readings`` 里每一项是一组字段覆盖值；空表示这件事连读法都还没有
    （用户压根没提），那就只能问。
    """

    field: str
    readings: tuple[Mapping[str, object], ...]
    assumption: str | None = None


@dataclass(frozen=True, slots=True)
class DivergenceVerdict:
    """哪些事不必问、哪些必须问。"""

    settled: dict[str, object]
    must_ask: tuple[str, ...]
    assumptions: tuple[str, ...]


#: 还没定下来的一格。留着占位而不是整份作废，是为了让"换一种读法轮廓变不变"
#: 这个判断**不依赖别的字段有没有定**：出发地是不是同一座城市，和到达时限还没问到
#: 是两件事，不该互相牵连。
UNSETTLED = "?"


def sketch(payload: SketchInput) -> PlanSketch:
    """算出行程轮廓；还没定的格子留占位符，整份轮廓仍然可比。"""
    legs = [
        (
            payload.origin or UNSETTLED,
            payload.destination or UNSETTLED,
            payload.departure_after.isoformat()
            if payload.departure_after is not None
            else UNSETTLED,
            payload.arrive_by.isoformat() if payload.arrive_by is not None else UNSETTLED,
        )
    ]
    if (
        payload.booking_scope is BookingScope.ROUND_TRIP
        and payload.return_after is not None
        and payload.return_before is not None
    ):
        legs.append(
            (
                payload.return_origin or payload.destination or UNSETTLED,
                payload.origin or UNSETTLED,
                payload.return_after.isoformat(),
                payload.return_before.isoformat(),
            )
        )
    lodging = None
    if payload.hotel_check_in is not None and payload.hotel_check_out is not None:
        lodging = (payload.hotel_check_in.isoformat(), payload.hotel_check_out.isoformat())
    elif payload.lodging_required:
        # 说了要住却还没有日期，是一份**未完成**的轮廓，不能和"住哪几天已经定了"
        # 算成同一份——否则一件真的没定的事会被判成"不影响结论"。
        lodging = (UNSETTLED, UNSETTLED)
    return PlanSketch(
        legs=tuple(legs),
        lodging=lodging,
        requirements=tuple(sorted(payload.requirements)),
    )


def resolve_open_questions(
    base: SketchInput, questions: list[OpenQuestion]
) -> DivergenceVerdict:
    """把每件没定的事的读法各算一遍轮廓，只留下真会改变轮廓的那些去问。"""
    settled: dict[str, object] = {}
    must_ask: list[str] = []
    assumptions: list[str] = []

    # 先把只有一种读法的事定下来：它们没有分歧可言，而且会让后面的轮廓更完整。
    baseline = base
    for question in questions:
        if len(question.readings) == 1:
            baseline = replace(baseline, **dict(question.readings[0]))

    for question in questions:
        if not question.readings:
            must_ask.append(question.field)
            continue
        sketches = {
            sketch(replace(baseline, **dict(reading))) for reading in question.readings
        }
        if len(sketches) > 1:
            must_ask.append(question.field)
            continue
        settled.update(question.readings[0])
        if question.assumption:
            assumptions.append(question.assumption)

    return DivergenceVerdict(
        settled=settled,
        must_ask=tuple(dict.fromkeys(must_ask)),
        assumptions=tuple(dict.fromkeys(assumptions)),
    )
