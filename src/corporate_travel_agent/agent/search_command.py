"""Compile semantic travel meaning into validated executable search commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from corporate_travel_agent.domain.constraints import WHOLE_JOURNEY_ONLY_REQUIREMENTS
from corporate_travel_agent.domain.enums import (
    BookingScope,
    LodgingRequirement,
    TripLegRole,
)
from corporate_travel_agent.domain.models import (
    Commitment,
    ScopedRequirement,
    TripLeg,
    TripRequestVersion,
)
from corporate_travel_agent.domain.validation import validate_trip_request
from corporate_travel_agent.planning.divergence import (
    OpenQuestion,
    SketchInput,
    resolve_open_questions,
)
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
    # 宿主替旅行者定下来、并且必须当面说出口的事（"这两个名字是同一座城市"）。
    assumptions: tuple[str, ...] = ()

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

    # 先按分歧筛一遍：几种读法算出同一份行程轮廓的事，答案不改变结论，不问。
    verdict = _resolve_by_divergence(intent, city_normalizer)
    missing.extend(verdict.must_ask)
    assumptions = list(verdict.assumptions)
    origin = verdict.settled.get("origin")
    destination = verdict.settled.get("destination")
    return_origin = verdict.settled.get("return_origin")
    settled_check_in = verdict.settled.get("hotel_check_in")
    settled_check_out = verdict.settled.get("hotel_check_out")

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

    hotel_check_in = intent.hotel_check_in or settled_check_in
    hotel_check_out = intent.hotel_check_out or settled_check_out
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
            assumptions=tuple(assumptions),
        )

    assert isinstance(origin, str) and isinstance(destination, str)
    journey = _journey_from(
        intent, city_normalizer, origin, destination, return_origin
    )
    scoped_hard = _scoped_from(
        hard_constraints, intent.leg_scoped_hard_constraints, len(journey)
    )
    scoped_soft = _scoped_from(
        intent.soft_preferences, intent.leg_scoped_soft_preferences, len(journey)
    )
    request = TripRequestVersion(
        task_id=task_id,
        version=version,
        traveler_id=traveler_id,
        origin=origin,
        destination=destination,
        departure_after=intent.departure_after,
        arrive_by=intent.arrive_by,
        return_after=intent.return_after,
        return_before=intent.return_before,
        hotel_check_in=hotel_check_in,
        hotel_check_out=hotel_check_out,
        hard_constraints=_requirement_names(scoped_hard),
        soft_preferences=_requirement_names(scoped_soft),
        scoped_hard_constraints=scoped_hard,
        scoped_soft_preferences=scoped_soft,
        booking_scope=intent.booking_scope,
        journey=journey,
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
            assumptions=tuple(assumptions),
        )
    return SearchCommandCompilation(
        command=SearchCommand(request=request), assumptions=tuple(assumptions)
    )




def _resolve_by_divergence(intent: Any, city_normalizer: CityNormalizer) -> Any:
    """把没定下来的事按「会不会改变行程轮廓」筛一遍，只留真分歧去问。

    两件今天会白问一次的事：

    - 旅行者写了"上海"、系统里叫 Shanghai，模型如实给了两个候选。它们**是同一座
      城市**，问"你说的是哪个"没有意义。
    - 说了要住酒店，但没说哪天到哪天——往返行程已经把到达日和返程日钉死了，
      入住退房只有一种算法。这是算术，不是替他编日期，所以照算并当面说出口。
    """
    base = SketchInput(
        origin=None,
        destination=None,
        return_origin=None,
        departure_after=intent.departure_after,
        arrive_by=intent.arrive_by,
        return_after=intent.return_after,
        return_before=intent.return_before,
        booking_scope=intent.booking_scope,
        lodging_required=intent.lodging_requirement is LodgingRequirement.REQUIRED,
        hotel_check_in=intent.hotel_check_in,
        hotel_check_out=intent.hotel_check_out,
        requirements=frozenset(intent.hard_constraints) | frozenset(intent.soft_preferences),
    )
    questions = [
        _city_question("origin", intent.origin_candidates, city_normalizer),
        _city_question("destination", intent.destination_candidates, city_normalizer),
    ]
    if intent.return_origin_candidates:
        questions.append(
            _city_question(
                "return_origin", intent.return_origin_candidates, city_normalizer
            )
        )
    questions.append(_lodging_dates_question(intent))
    return resolve_open_questions(base, [item for item in questions if item is not None])


def _city_question(
    field: str, candidates: list[str], city_normalizer: CityNormalizer
) -> OpenQuestion:
    """一座城市的几种叫法各算一遍轮廓；规范化后是同一座就不算分歧。"""
    readings = tuple(
        {field: name}
        for name in dict.fromkeys(
            city_normalizer.canonicalize(item) for item in candidates
        )
    )
    assumption = None
    if len(readings) == 1 and len(candidates) > 1:
        assumption = (
            f"把「{'」「'.join(candidates)}」当作同一座城市"
            f"{readings[0][field]} 处理。"
        )
    return OpenQuestion(field=field, readings=readings, assumption=assumption)


def _lodging_dates_question(intent: Any) -> OpenQuestion | None:
    """要住酒店但没说日期时，看行程本身有没有把它钉死。"""
    if intent.lodging_requirement is not LodgingRequirement.REQUIRED:
        return None
    if intent.hotel_check_in is not None and intent.hotel_check_out is not None:
        return None
    arrive_by = intent.arrive_by
    return_after = intent.return_after
    if not isinstance(arrive_by, datetime) or not isinstance(return_after, datetime):
        return OpenQuestion(field="hotel_check_in", readings=())
    check_in = arrive_by.date()
    check_out = return_after.date()
    if check_out <= check_in:
        # 当天往返却说要住店：这不是算得出来的事，是真的要问。
        return OpenQuestion(field="hotel_check_in", readings=())
    return OpenQuestion(
        field="hotel_check_in",
        readings=({"hotel_check_in": check_in, "hotel_check_out": check_out},),
        assumption=(
            f"按行程推算住宿为 {check_in.isoformat()} 入住、"
            f"{check_out.isoformat()} 退房。"
        ),
    )


def _requirement_names(scoped: tuple[ScopedRequirement, ...]) -> tuple[str, ...]:
    """带作用域的要求对应的扁平名字视图（去重，保留首次出现顺序）。"""
    return tuple(dict.fromkeys(item.name for item in scoped))


def _scoped_from(
    names: list[str],
    leg_scoped: list[Any],
    leg_count: int,
) -> tuple[ScopedRequirement, ...]:
    """把"名字列表 + 谁只管哪一段"编译成带作用域的要求。

    契约只有一条：名字仍然由扁平列表说了算，这两个数组只负责**收窄**它。
    一个名字只要在 leg_scoped 里出现过，它就只管被点名的那几段；没出现过就管全程。
    这样政策引擎、评测集和旧持久化载荷读到的扁平列表始终是完整的一批名字，
    不会因为加了作用域而丢东西。

    只谈整趟行程的名字（"住得离客户近"、"最便宜"）被标上航段号时按整趟理解：
    给它们标段号是把话说错了，唯一说得通的读法只有一种，没有必要为此回头追问。
    """
    narrowed: dict[str, list[int]] = {}
    for item in leg_scoped:
        name = getattr(item, "name", None)
        leg_index = getattr(item, "leg_index", None)
        if not isinstance(name, str) or not isinstance(leg_index, int):
            continue
        if name in WHOLE_JOURNEY_ONLY_REQUIREMENTS:
            continue
        narrowed.setdefault(name, []).append(leg_index)
    ordered = list(dict.fromkeys([*names, *narrowed]))
    compiled: list[ScopedRequirement] = []
    for name in ordered:
        indices = sorted(set(narrowed.get(name, ())))
        # 一条要求管遍了每一段，和"管全程"是同一件事，不必留着段号让下游多想一层。
        if not indices or len(indices) == leg_count:
            compiled.append(ScopedRequirement(name=name))
            continue
        compiled.extend(ScopedRequirement(name=name, leg_index=index) for index in indices)
    return tuple(compiled)


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




def _journey_from(
    intent: Any,
    city_normalizer: CityNormalizer,
    origin: str,
    destination: str,
    return_origin: object = None,
) -> tuple[TripLeg, ...]:
    """把语义理解编译成有序航段。

    返程的**起点**用旅行者说过的那座城市；没说就是原路返回。此前领域模型没有地方
    放"从别的城市回"，于是这句话被无声丢掉，宿主按"目的地→出发地"拼出一条用户
    没要过的航线并真的去搜了库存。现在它只是第二段自己的 origin，不再是特例。
    """
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
        legs.append(
            TripLeg(
                role=TripLegRole.RETURN,
                origin=(
                    return_origin
                    if isinstance(return_origin, str) and return_origin
                    else destination
                ),
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
    """**问什么由宿主定，怎么问才交给模型。**

    此前这里无条件让位给模型那一句追问。模型一次只问一件事，于是一段五轮的对话
    被拆成一轮一个字段问了五次——其中好几个问题的答案根本不改变最终推荐哪班车。

    但**盖掉模型那一句也是错的**：只有它说得出这次到底哪里有歧义——
    "这周五还是下周五"这种话，宿主手上只有一个字段名 `departure_after`，
    再怎么措辞也问不出那个"周五"。所以两边各管各的：模型那句照旧打头，
    宿主把还差的事补在后面，一次说完。
    """
    if any(item.startswith(PAST_DATE_PREFIX) for item in conflicts):
        return _clarification_for(missing, conflicts)
    if not model_question:
        return _clarification_for(missing, conflicts)
    if len(missing) <= 1:
        return model_question
    return model_question + " " + _still_needed(missing)


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


#: 缺口字段的中文说法。宿主自己开口时不该把内部字段名摔在用户脸上。
_FIELD_LABELS = {
    "origin": "从哪出发",
    "destination": "去哪",
    "departure_after": "哪天出发",
    "arrive_by": "最晚什么时候要到",
    "return_after": "哪天返程",
    "return_before": "返程最晚什么时候到",
    "return_origin": "返程从哪出发",
    "hotel_check_in": "酒店哪天入住",
    "hotel_check_out": "酒店哪天退房",
}



def _still_needed(missing: tuple[str, ...]) -> str:
    """宿主自己那半句：还差哪几件事。用日常说法，不把内部字段名摔在用户脸上。"""
    labels = [_FIELD_LABELS.get(item, item) for item in missing]
    return "另外我还需要知道：" + "、".join(labels) + "。"


def _clarification_for(missing: tuple[str, ...], conflicts: tuple[str, ...]) -> str:
    blocking = [item for item in conflicts if item.startswith(PAST_DATE_PREFIX)]
    if blocking:
        # 这类结论不是"再问一遍"能解决的歧义，直接把结论摆出来。
        return "；".join(blocking)
    if missing:
        return _still_needed(missing)
    if conflicts:
        return "我发现行程中还有会影响搜索的歧义，请确认：" + "；".join(conflicts) + "。"
    return "请确认我对这次出行的理解后再继续搜索。"
