"""Compile semantic travel meaning into validated executable search commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from corporate_travel_agent.domain.enums import (
    BookingScope,
    LodgingRequirement,
    TripLegRole,
)
from corporate_travel_agent.domain.models import (
    Commitment,
    TripLeg,
    TripRequestVersion,
)
from corporate_travel_agent.domain.validation import validate_trip_request
from corporate_travel_agent.services.locations import CityNormalizer

from .semantic_intent import (
    IntentDecision,
    IntentDecisionStatus,
    ungrounded_required_fields,
)


@dataclass(frozen=True, slots=True)
class SearchCommand:
    """Executable, provider-independent command accepted by the workflow."""

    request: TripRequestVersion


@dataclass(frozen=True, slots=True)
class SearchCommandCompilation:
    """Either one validated command or an explicit reason to pause."""

    command: SearchCommand | None
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    clarification_question: str | None = None

    @property
    def ready(self) -> bool:
        return self.command is not None and not self.missing and not self.conflicts


def compile_search_command(
    decision: IntentDecision,
    *,
    task_id: str,
    traveler_id: str,
    version: int,
    city_normalizer: CityNormalizer,
    created_at: datetime,
    already_grounded: frozenset[str] = frozenset(),
) -> SearchCommandCompilation:
    """Compile without guessing; unresolved semantics always produce clarification."""
    intent = decision.intent
    missing: list[str] = []
    conflicts = list(decision.conflicts)

    if len(intent.origin_candidates) != 1:
        missing.append("origin")
    if len(intent.destination_candidates) != 1:
        missing.append("destination")
    if intent.departure_after is None:
        missing.append("departure_after")
    if intent.arrive_by is None:
        missing.append("arrive_by")
    # 模型说可以查了，却拿不出某个必填字段的原话支撑 —— 当作还没问清，不是违约。
    missing.extend(
        field
        for field in ungrounded_required_fields(decision)
        if field not in already_grounded
    )
    if len(intent.return_origin_candidates) > 1:
        # 返程从哪出发本身还没定下来，先问清再说。
        missing.append("return_origin")
    past = _dates_already_passed(intent, created_at)
    conflicts.extend(past)
    if intent.uncertainties:
        conflicts.extend(f"unresolved uncertainty: {item}" for item in intent.uncertainties)
    if intent.conditions:
        conflicts.extend(f"unresolved condition: {item}" for item in intent.conditions)
    if decision.status is IntentDecisionStatus.UNSUPPORTED:
        conflicts.extend(decision.unsupported_reasons or ["unsupported request"])
    elif decision.status is IntentDecisionStatus.OUT_OF_SCOPE:
        conflicts.append("request is outside corporate travel planning scope")

    hotel_check_in = intent.hotel_check_in
    hotel_check_out = intent.hotel_check_out
    hard_constraints = list(intent.hard_constraints)
    if intent.lodging_requirement is LodgingRequirement.REQUIRED:
        hard_constraints = list(dict.fromkeys([*hard_constraints, "hotel_required"]))
        if hotel_check_in is None:
            missing.append("hotel_check_in")
        if hotel_check_out is None:
            missing.append("hotel_check_out")
    elif hotel_check_in is not None or hotel_check_out is not None:
        conflicts.append("hotel dates are present but lodging requirement is not confirmed")
    else:
        hotel_check_in = None
        hotel_check_out = None
        hard_constraints = [item for item in hard_constraints if item != "hotel_required"]

    missing_tuple = tuple(dict.fromkeys(missing))
    conflicts_tuple = tuple(dict.fromkeys(item for item in conflicts if item))
    if (
        decision.status is not IntentDecisionStatus.READY
        or missing_tuple
        or conflicts_tuple
    ):
        return SearchCommandCompilation(
            command=None,
            missing=missing_tuple,
            conflicts=conflicts_tuple,
            clarification_question=_question_for(
                decision.clarification_question, missing_tuple, conflicts_tuple
            ),
        )

    request = TripRequestVersion(
        task_id=task_id,
        version=version,
        traveler_id=traveler_id,
        origin=city_normalizer.canonicalize(intent.origin_candidates[0]),
        destination=city_normalizer.canonicalize(intent.destination_candidates[0]),
        departure_after=intent.departure_after,
        arrive_by=intent.arrive_by,
        return_after=intent.return_after,
        return_before=intent.return_before,
        hotel_check_in=hotel_check_in,
        hotel_check_out=hotel_check_out,
        hard_constraints=tuple(hard_constraints),
        soft_preferences=tuple(intent.soft_preferences),
        booking_scope=intent.booking_scope,
        journey=_journey_from(intent, city_normalizer),
        commitments=_commitments_from(intent, hard_constraints),
        client_location=intent.client_location,
        created_at=created_at,
    )
    validation = validate_trip_request(request)
    if not validation.valid:
        return SearchCommandCompilation(
            command=None,
            missing=validation.missing,
            conflicts=validation.conflicts,
            clarification_question=_question_for(
                decision.clarification_question, validation.missing, validation.conflicts
            ),
        )
    return SearchCommandCompilation(command=SearchCommand(request=request))


def _commitments_from(intent: Any, hard_constraints: list[str]) -> tuple[Commitment, ...]:
    """把「要在哪、什么时候之前到场、为了什么」从散落的字段里收拢成承诺。

    今天这三样分别躺在 `arrive_by`（时限）、`client_location`（地点，随后被丢掉）和
    `hard_constraints` 里的字符串 `arrive_before_meeting`（要不要留缓冲）。
    收拢之后它们才是一件事，政策也才谈得上判断这趟差旅本身合不合理。
    """
    if intent.arrive_by is None:
        return ()
    place = (intent.client_location or "").strip() or (
        intent.destination_candidates[0] if len(intent.destination_candidates) == 1 else ""
    )
    if not place:
        return ()
    return (
        Commitment(
            place=place,
            not_later_than=intent.arrive_by,
            purpose=intent.summary or None,
            safety_buffer_required="arrive_before_meeting" in hard_constraints,
        ),
    )




def _journey_from(intent: Any, city_normalizer: CityNormalizer) -> tuple[TripLeg, ...]:
    """把语义理解编译成有序航段。

    返程的**起点**用旅行者说过的那座城市；没说就是原路返回。此前领域模型没有地方
    放"从别的城市回"，于是这句话被无声丢掉，宿主按"目的地→出发地"拼出一条用户
    没要过的航线并真的去搜了库存。现在它只是第二段自己的 origin，不再是特例。
    """
    origin = city_normalizer.canonicalize(intent.origin_candidates[0])
    destination = city_normalizer.canonicalize(intent.destination_candidates[0])
    outbound_role = (
        TripLegRole.RETURN
        if intent.booking_scope is BookingScope.RETURN_ONLY
        else TripLegRole.OUTBOUND
    )
    legs = [
        TripLeg(
            role=outbound_role,
            origin=origin,
            destination=destination,
            depart_after=intent.departure_after,
            arrive_before=intent.arrive_by,
        )
    ]
    if (
        intent.booking_scope is BookingScope.ROUND_TRIP
        and intent.return_after is not None
        and intent.return_before is not None
    ):
        return_origin = (
            city_normalizer.canonicalize(intent.return_origin_candidates[0])
            if len(intent.return_origin_candidates) == 1
            else destination
        )
        legs.append(
            TripLeg(
                role=TripLegRole.RETURN,
                origin=return_origin,
                destination=origin,
                depart_after=intent.return_after,
                arrive_before=intent.return_before,
            )
        )
    return tuple(legs)


PAST_DATE_PREFIX = "日期已过"


def _question_for(
    model_question: str | None, missing: tuple[str, ...], conflicts: tuple[str, ...]
) -> str:
    """日期已过时用宿主的确定性结论，其余情况优先用模型自己的追问。"""
    if any(item.startswith(PAST_DATE_PREFIX) for item in conflicts):
        return _clarification_for(missing, conflicts)
    return model_question or _clarification_for(missing, conflicts)


def _dates_already_passed(intent: Any, now: datetime) -> list[str]:
    """出行日期落在今天之前就直接报错，绝不悄悄顺延到下一年。

    比的是**日历日**而不是精确时刻：用户说"8月5日"指的是那一天，
    而不是那天零点这个瞬间。
    """
    problems: list[str] = []
    for label, value in (
        ("出发日期", intent.departure_after),
        ("返程日期", intent.return_after),
    ):
        if not isinstance(value, datetime):
            continue
        today = now.astimezone(value.tzinfo).date() if value.tzinfo else now.date()
        if value.date() < today:
            problems.append(
                f"{PAST_DATE_PREFIX}：{label} {value.date().isoformat()} "
                f"在今天（{today.isoformat()}）之前，请改成今天之后的日期。"
            )
    # 到达时限按**精确时刻**比：截止时间一旦过去，这趟行程已经不可能成立，
    # 和它是不是"今天"无关。这也是"下午还去订上午必须到的票"的那一半。
    arrive_by = intent.arrive_by
    if isinstance(arrive_by, datetime) and arrive_by <= now:
        problems.append(
            f"{PAST_DATE_PREFIX}：要求的到达时限 {arrive_by.isoformat()} 已经过了，"
            "请给一个还没到的时间。"
        )
    return problems


def _clarification_for(missing: tuple[str, ...], conflicts: tuple[str, ...]) -> str:
    blocking = [item for item in conflicts if item.startswith(PAST_DATE_PREFIX)]
    if blocking:
        # 这类结论不是"再问一遍"能解决的歧义，直接把结论摆出来。
        return "；".join(blocking)
    if missing:
        return "请确认这些出行信息后我再搜索：" + "、".join(missing) + "。"
    if conflicts:
        return "我发现行程中还有会影响搜索的歧义，请确认：" + "；".join(conflicts) + "。"
    return "请确认我对这次出行的理解后再继续搜索。"
