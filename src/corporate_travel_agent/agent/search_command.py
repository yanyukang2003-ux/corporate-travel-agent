"""Compile semantic travel meaning into validated executable search commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from corporate_travel_agent.domain.enums import LodgingRequirement
from corporate_travel_agent.domain.models import TripRequestVersion
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
    missing.extend(ungrounded_required_fields(decision))
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
    return problems


def _clarification_for(missing: tuple[str, ...], conflicts: tuple[str, ...]) -> str:
    passed = [item for item in conflicts if item.startswith(PAST_DATE_PREFIX)]
    if passed:
        # 日期已过不是"再问一遍"能解决的歧义，直接把结论摆出来。
        return "；".join(passed)
    if missing:
        return "请确认这些出行信息后我再搜索：" + "、".join(missing) + "。"
    if conflicts:
        return "我发现行程中还有会影响搜索的歧义，请确认：" + "；".join(conflicts) + "。"
    return "请确认我对这次出行的理解后再继续搜索。"
