"""clarification_questions：结构化用户澄清提示（AskUserQuestion 风格）。

受 Anthropic Claude Code ``AskUserQuestionTool`` 启发：

- 在副作用（库存搜索）之前，参数缺失或歧义时先提问。
- 每题优先 2–4 个具体选项；自由文本仍可通过 submit_message 有效。
- 从不编造城市或出行日——只收集有依据的回答。

亦对齐 Claude 参数环：L2 业务不完整 → CLARIFY，而非静默补全。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from corporate_travel_agent.agent.journey_semantics import booking_scope_for_fields
from corporate_travel_agent.domain.enums import BookingScope, LodgingRequirement

# TripTask.metadata 中的澄清相关键
CLARIFICATION_QUESTIONS_KEY = "clarification_questions"
CLARIFICATION_PENDING_KEY = "clarification_pending_slots"
CLARIFICATION_ANSWERS_KEY = "clarification_answers"

_RETURN_MENTION_RE = re.compile(
    r"("
    r"return|round[\s-]?trip|come\s+back|going\s+back|fly\s+back|"
    r"返程|回来|回程|当天下午回|当天回|第二天回|隔天回|往返|"
    r"下午回|晚上回|明天回|\d{1,2}[日号]回"
    r")",
    re.I,
)
_ONE_WAY_RE = re.compile(
    r"(one[\s-]?way|single\s+leg|no\s+return|不返程|单程|不去回)",
    re.I,
)
_HOTEL_RE = re.compile(
    r"(hotel|lodging|accommodation|check[ -]?in|check[ -]?out|"
    r"酒店|住宿|入住|退房|住一晚|住两晚|订一晚|订两晚|住几晚)",
    re.I,
)
_NO_HOTEL_RE = re.compile(
    r"("
    r"no\s+hotel|without\s+hotel|don'?t\s+need\s+(a\s+)?hotel|"
    r"不住酒店|不要酒店|酒店不要了|无需酒店|不用酒店|不需要酒店|不安排酒店"
    r")",
    re.I,
)
_FLIGHT_RE = re.compile(r"(flight|plane|air|飞机|航班|民航)", re.I)
_TRAIN_RE = re.compile(r"(train|rail|高铁|动车|火车|railway)", re.I)
_CLOCK_ANSWER_RE = re.compile(
    r"^(?:最晚)?(?:到达|抵达|arrive(?:\s+by)?)?\s*"
    r"(?P<period>早上|上午|中午|下午|傍晚|晚上|今晚)?"
    r"\s*"
    r"(?:(?P<hour>\d{1,2})|(?P<cn>十一|十二|十|[一二两三四五六七八九]))"
    r"\s*(?:点|點|:|：)"
    r"(?P<minute>\d{2})?"
    r"\s*(?:之前|以前|前|到)?"
    r"\s*(?:到达|抵达)?"
    r"$",
    re.I,
)
_CN_HOUR = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}

KNOWN_OPTION_VALUES = frozenset(
    {
        "return:yes",
        "return:no",
        "return:same_day_afternoon",
        "hotel:required",
        "hotel:skip",
        "hotel:none",
        "hotel:one_night",
        "mode:flight_only",
        "mode:train_only",
        "mode:any",
        "template:day_trip",
        "template:overnight",
        "arrive:12:00",
        "arrive:18:00",
        "arrive:21:00",
        "depart:08:00",
        "depart:12:00",
        "depart:14:00",
        "window:08:00-18:00",
        "window:08:00-21:00",
        "window:12:00-18:00",
        "window:12:00-21:00",
        "client:skip",
        "route:Beijing:Shanghai",
        "route:Shanghai:Beijing",
        "free_text",
    }
)

# (canonical, 中文标签, 别名) — 澄清选项与自由文本城市落地共用。
CLARIFICATION_CITY_CHOICES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Beijing", "北京", ("beijing", "北京", "北京市", "pek", "bjs", "cn-bjs")),
    ("Shanghai", "上海", ("shanghai", "上海", "上海市", "sha", "pvg", "cn-sha")),
    ("Hong Kong", "香港", ("hong kong", "hongkong", "香港", "hkg")),
    ("Tokyo", "东京", ("tokyo", "东京", "東京", "tyo", "nrt", "hnd")),
    ("Singapore", "新加坡", ("singapore", "新加坡", "sin")),
    ("New York", "纽约", ("new york", "newyork", "nyc", "纽约", "紐約", "jfk")),
    ("Los Angeles", "洛杉矶", ("los angeles", "losangeles", "la", "洛杉矶", "洛杉磯", "lax")),
    ("Chicago", "芝加哥", ("chicago", "芝加哥", "chi", "ord")),
    ("London", "伦敦", ("london", "伦敦", "倫敦", "lon", "lhr")),
    ("San Francisco", "旧金山", ("san francisco", "sanfrancisco", "sf", "旧金山", "三藩市", "sfo")),
    ("Boston", "波士顿", ("boston", "波士顿", "波士頓", "bos")),
    ("Washington", "华盛顿", ("washington", "washington dc", "华盛顿", "華盛頓", "was")),
)

_CITY_OPTION_RE = re.compile(r"^(origin|destination|city):(.+)$", re.I)
_CLOCK_OPTION_RE = re.compile(r"^(depart|arrive):(\d{1,2}):(\d{2})$", re.I)
_WINDOW_OPTION_RE = re.compile(r"^window:(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", re.I)
_ISO_RANGE_RE = re.compile(
    r"^(?P<kind>window|return_window):"
    r"(?P<start>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})"
    r"/"
    r"(?P<end>\d{4}-\d{2}-\d{2}T\d{2}:\d{2})$"
)
_DATE_OPTION_RE = re.compile(r"^date:(\d{4}-\d{2}-\d{2})$")


def lookup_clarification_city(raw: str) -> str | None:
    """把用户输入或选项值解析为规范城市名；无法识别则 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    folded = text.casefold()
    for canonical, label, aliases in CLARIFICATION_CITY_CHOICES:
        names = {canonical.casefold(), label.casefold(), *(item.casefold() for item in aliases)}
        if folded in names:
            return canonical
    return None


def is_exact_option_value(message: str) -> bool:
    """消息是否为 UI 选项机值（非整句自然语言）。"""
    compact = (message or "").strip()
    if compact in KNOWN_OPTION_VALUES:
        return True
    if _CITY_OPTION_RE.fullmatch(compact):
        return True
    if _CLOCK_OPTION_RE.fullmatch(compact):
        return True
    if _WINDOW_OPTION_RE.fullmatch(compact):
        return True
    if _ISO_RANGE_RE.fullmatch(compact):
        return True
    if _DATE_OPTION_RE.fullmatch(compact):
        return True
    return False


@dataclass(frozen=True, slots=True)
class QuestionOption:
    """澄清题的一个可选项（展示文案 + 机器 value）。"""

    label: str
    description: str
    # 选中后写入意图的机器值（UI 不只展示该字符串）。
    value: str


@dataclass(frozen=True, slots=True)
class ClarificationQuestion:
    """面向出行人的一道多选（或自由文本）澄清题。"""

    id: str
    header: str
    question: str
    options: tuple[QuestionOption, ...]
    multi_select: bool = False
    # 本题意图解决的意图槽位。
    slots: tuple[str, ...] = ()
    # date = 日期选择；time_range = 可点开的日期+起止时间区间。
    input_kind: str = ""

    def as_dict(self) -> dict[str, Any]:
        """序列化为 API/元数据可用的字典。"""
        return {
            "id": self.id,
            "header": self.header,
            "question": self.question,
            "multi_select": self.multi_select,
            "slots": list(self.slots),
            "options": [asdict(option) for option in self.options],
            "input_kind": self.input_kind,
        }


@dataclass(frozen=True, slots=True)
class ClarificationBundle:
    """一组澄清题 + 缺失/不确定槽 + 拼好的 prompt 文本。"""

    questions: tuple[ClarificationQuestion, ...]
    missing: tuple[str, ...]
    uncertain: tuple[str, ...]
    prompt_text: str

    def as_dict(self) -> dict[str, Any]:
        """序列化整组澄清包。"""
        return {
            "questions": [item.as_dict() for item in self.questions],
            "missing": list(self.missing),
            "uncertain": list(self.uncertain),
            "prompt_text": self.prompt_text,
        }


_FIELD_LABELS = {
    "origin": "出发城市",
    "destination": "目的城市",
    "departure_after": "最早出发时间",
    "arrive_by": "最晚到达/会议时间",
    "return_after": "返程最早出发",
    "return_before": "返程最晚到达",
    "hotel_check_in": "酒店入住日",
    "hotel_check_out": "酒店退房日",
    "return_trip": "是否需要返程",
    "hotel_need": "是否需要酒店",
    "transport_mode": "交通方式",
    "client_location": "客户公司位置",
    "travel_date": "出行日期",
}


def message_mentions_return(text: str) -> bool:
    """用户文本是否提到返程/回来。"""
    return bool(_RETURN_MENTION_RE.search(text or ""))


def message_mentions_one_way(text: str) -> bool:
    """用户文本是否明确单程/不返程。"""
    return bool(_ONE_WAY_RE.search(text or ""))


def message_mentions_hotel(text: str) -> bool:
    """是否提到酒店需求（显式不要酒店则 False）。"""
    text = text or ""
    if _NO_HOTEL_RE.search(text):
        return False
    return bool(_HOTEL_RE.search(text))


def assumptions_mention_return(assumptions: tuple[str, ...] | list[str]) -> bool:
    """模型 assumptions 是否含返程暗示。"""
    return any(message_mentions_return(item) for item in assumptions)


def detect_uncertain_slots(
    fields: dict[str, Any],
    *,
    user_message: str,
    assumptions: tuple[str, ...] | list[str] = (),
    classification: str = "TRIP",
) -> tuple[str, ...]:
    """库存搜索前值得追问的歧义槽。

    不一定是硬缺失；例如单程可不填返程，但用户明确说了回来且字段为空时要问。
    """
    if classification == "OUT_OF_SCOPE":
        return ()

    uncertain: list[str] = []
    hard = set(fields.get("hard_constraints") or ())
    soft = set(fields.get("soft_preferences") or ())

    has_return = fields.get("return_after") is not None or fields.get("return_before") is not None
    from corporate_travel_agent.agent.intent_calibration import (
        iter_invalid_date_tokens,
        message_has_ambiguous_weekday_choice,
        message_is_lunar_or_holiday_without_gregorian,
    )

    if iter_invalid_date_tokens(user_message):
        uncertain.append("travel_date")

    # 只说「从北京回来」已经是在安排返程，不要再问「需不需要返程 / 只要去程」。
    return_leg_only = (
        booking_scope_for_fields(fields, user_message=user_message)
        is BookingScope.RETURN_ONLY
    )
    return_talk = message_mentions_return(user_message) and not message_mentions_one_way(
        user_message
    )
    if return_leg_only:
        pass
    elif return_talk and not has_return:
        uncertain.append("return_trip")
    elif (
        not has_return
        and assumptions_mention_return(assumptions)
        and message_mentions_return(user_message)
    ):
        uncertain.append("return_trip")

    hotel_required = "hotel_required" in hard
    has_hotel_dates = (
        fields.get("hotel_check_in") is not None or fields.get("hotel_check_out") is not None
    )
    # 能力契约：追问已抽出偏好的支撑槽。
    # 此处不看用户措辞——须由抽取/L0 写入偏好。
    from corporate_travel_agent.agent.capability_contract import completeness_gaps

    uncertain.extend(completeness_gaps(fields))

    # 仅当用户文本落实了酒店需求但缺日期时追问。
    if (
        message_mentions_hotel(user_message)
        and not has_hotel_dates
        and not hotel_required
    ):
        uncertain.append("hotel_need")

    mode_locked = "train_only" in hard or "flight_only" in hard
    flight_talk = bool(_FLIGHT_RE.search(user_message or ""))
    train_talk = bool(_TRAIN_RE.search(user_message or ""))
    if not mode_locked and flight_talk and train_talk:
        if "compare_train_and_flight" not in soft:
            uncertain.append("transport_mode")
    elif not mode_locked and train_talk and not flight_talk:
        # 仅提火车且无硬锁仍可搜；仅当软偏好冲突（prefer_flight+火车文）时追问。
        if "prefer_flight" in soft:
            uncertain.append("transport_mode")
    elif not mode_locked and flight_talk and "prefer_train" in soft:
        uncertain.append("transport_mode")

    has_outbound = fields.get("departure_after") is not None or fields.get("arrive_by") is not None
    if not has_outbound and (
        message_has_ambiguous_weekday_choice(user_message)
        or message_is_lunar_or_holiday_without_gregorian(user_message)
    ):
        uncertain.append("travel_date")

    return tuple(dict.fromkeys(uncertain))


def _city_question(
    missing: tuple[str, ...],
    fields: dict[str, Any],
    *,
    return_leg_only: bool = False,
) -> ClarificationQuestion:
    """出发/目的城市澄清：目录城市可点选，不再只给空白输入框。"""
    origin_missing = "origin" in missing
    dest_missing = "destination" in missing
    known_origin = fields.get("origin") if isinstance(fields.get("origin"), str) else None
    known_dest = fields.get("destination") if isinstance(fields.get("destination"), str) else None
    options: list[QuestionOption] = []
    for canonical, label, _aliases in CLARIFICATION_CITY_CHOICES:
        folded = canonical.casefold()
        if origin_missing and not dest_missing:
            if known_dest and str(known_dest).casefold() == folded:
                continue
            options.append(
                QuestionOption(label, f"出发城市 {label}", f"origin:{canonical}")
            )
        elif dest_missing and not origin_missing:
            if known_origin and str(known_origin).casefold() == folded:
                continue
            dest_prefix = "回到" if return_leg_only else "目的城市"
            options.append(
                QuestionOption(label, f"{dest_prefix} {label}", f"destination:{canonical}")
            )
        else:
            options.append(
                QuestionOption(label, f"先填出发城市 {label}", f"city:{canonical}")
            )
    if origin_missing and dest_missing:
        question = "请选择出发城市；目的城市选完出发后会再问一次。"
    elif origin_missing:
        dest_label = known_dest or "已识别目的地"
        question = f"请选择出发城市。当前目的地是 {dest_label}。"
    elif return_leg_only:
        origin_label = known_origin or "已识别出发地"
        question = (
            f"请选择回到哪座城市。当前是从 {origin_label} 返回，"
            f"不是把 {origin_label} 当作目的地。"
        )
    else:
        origin_label = known_origin or "已识别出发地"
        question = f"请选择目的城市。当前出发地是 {origin_label}。"
    return ClarificationQuestion(
        id="cities",
        header="城市",
        question=question,
        options=tuple(options),
        slots=tuple(name for name in ("origin", "destination") if name in missing),
    )


def _time_questions(
    time_missing: tuple[str, ...],
    *,
    return_leg_only: bool = False,
    invalid_tokens: tuple[str, ...] = (),
) -> list[ClarificationQuestion]:
    """时间澄清：可点开的日期+起止区间，常用窗口只作快捷项。"""
    questions: list[ClarificationQuestion] = []
    need_depart = "departure_after" in time_missing
    need_arrive = "arrive_by" in time_missing
    need_return = "return_after" in time_missing or "return_before" in time_missing
    invalid_note = ""
    if invalid_tokens:
        shown = "、".join(invalid_tokens)
        invalid_note = f"{shown} 不是有效公历日期。请改选真实日期，再选定时间区间。"

    if return_leg_only:
        questions.append(
            ClarificationQuestion(
                id="times",
                header="返程航段时间",
                question=invalid_note or "请选择这段返程的出发日期，以及出发和到达时间区间。",
                options=(),
                slots=("departure_after", "arrive_by"),
                input_kind="time_range",
            )
        )
        return questions

    if need_return and not need_depart and not need_arrive:
        questions.append(
            ClarificationQuestion(
                id="return_times",
                header="返程时间",
                question=invalid_note or "请选择返程日期，以及出发和到达时间区间。",
                options=(),
                slots=("return_after", "return_before"),
                input_kind="time_range",
            )
        )
    if need_depart or need_arrive:
        questions.append(
            ClarificationQuestion(
                id="times",
                header="时间",
                question=invalid_note or "请选择出发日期，以及最早出发到最晚到达的时间区间。",
                options=(),
                slots=tuple(
                    name for name in ("departure_after", "arrive_by") if name in time_missing
                )
                or ("departure_after", "arrive_by"),
                input_kind="time_range",
            )
        )
    return questions


def build_clarification_bundle(
    *,
    missing: tuple[str, ...] | list[str] = (),
    conflicts: tuple[str, ...] | list[str] = (),
    uncertain: tuple[str, ...] | list[str] = (),
    fields: dict[str, Any] | None = None,
    user_message: str = "",
) -> ClarificationBundle | None:
    """为缺失 + 不确定槽构建 AskUserQuestion 风格提示。"""
    from corporate_travel_agent.agent.intent_calibration import (
        iter_invalid_date_tokens,
    )

    missing = tuple(dict.fromkeys(missing))
    conflicts = tuple(dict.fromkeys(conflicts))
    uncertain = tuple(dict.fromkeys(uncertain))
    if not missing and not conflicts and not uncertain:
        return None

    questions: list[ClarificationQuestion] = []
    current = fields or {}
    return_leg_only = (
        booking_scope_for_fields(current, user_message=user_message)
        is BookingScope.RETURN_ONLY
    )
    invalid_tokens = iter_invalid_date_tokens(user_message)
    if return_leg_only:
        uncertain = tuple(item for item in uncertain if item != "return_trip")
        extra_missing = [
            name
            for name in ("departure_after", "arrive_by")
            if current.get(name) is None
        ]
        missing = tuple(dict.fromkeys([*missing, *extra_missing]))

    if "origin" in missing or "destination" in missing:
        questions.append(
            _city_question(missing, current, return_leg_only=return_leg_only)
        )

    time_missing = tuple(
        name
        for name in ("departure_after", "arrive_by", "return_after", "return_before")
        if name in missing
    )
    if invalid_tokens or "travel_date" in uncertain or time_missing:
        if invalid_tokens or time_missing:
            questions.extend(
                _time_questions(
                    time_missing
                    or (
                        ("departure_after", "arrive_by")
                        if return_leg_only
                        else ("departure_after", "arrive_by")
                    ),
                    return_leg_only=return_leg_only,
                    invalid_tokens=invalid_tokens,
                )
            )
        elif "travel_date" in uncertain:
            questions.append(
                ClarificationQuestion(
                    id="travel_date",
                    header="出行日期",
                    question=(
                        "请选择一个具体公历日期。"
                        "「这周五还是下周五」或农历/节假日无法直接换算出行日，系统不会猜。"
                    ),
                    options=(),
                    slots=("departure_after", "arrive_by"),
                    input_kind="date",
                )
            )

    hotel_missing = tuple(
        name for name in ("hotel_check_in", "hotel_check_out") if name in missing
    )
    if hotel_missing:
        questions.append(
            ClarificationQuestion(
                id="hotel_dates",
                header="酒店日期",
                question="政策要求酒店日期成对。请提供入住/退房日，或选择：",
                options=(
                    QuestionOption(
                        label="住一晚",
                        description="按会议日前一天入住、会议日退房（需已有到达日）",
                        value="hotel:one_night",
                    ),
                    QuestionOption(
                        label="不住酒店",
                        description="取消酒店要求，只规划交通",
                        value="hotel:none",
                    ),
                ),
                slots=hotel_missing,
            )
        )

    if "return_trip" in uncertain:
        questions.append(
            ClarificationQuestion(
                id="return_trip",
                header="返程",
                question="您提到返程/回来，但尚未确认返程时间。这次需要安排返程吗？",
                options=(
                    QuestionOption(
                        label="需要返程",
                        description="请接着补充返程最早出发与最晚到达时间",
                        value="return:yes",
                    ),
                    QuestionOption(
                        label="只要去程",
                        description="按单程搜索，不查返程",
                        value="return:no",
                    ),
                    QuestionOption(
                        label="当天下午回",
                        description="返程窗口默认会议日 13:00–18:00（本地时区）",
                        value="return:same_day_afternoon",
                    ),
                ),
                slots=("return_after", "return_before"),
            )
        )

    from corporate_travel_agent.agent.capability_contract import slot_spec

    for gap in uncertain:
        spec = slot_spec(gap)
        if spec is None:
            continue
        options = []
        if spec.skip_value and spec.skip_label:
            options.append(
                QuestionOption(
                    label=spec.skip_label,
                    description=spec.skip_description or "",
                    value=spec.skip_value,
                )
            )
        options.append(
            QuestionOption(
                label="我来填写",
                description="直接输入具体信息",
                value="free_text",
            )
        )
        questions.append(
            ClarificationQuestion(
                id=spec.name,
                header=spec.header,
                question=spec.question,
                options=tuple(options),
                slots=(spec.name,),
            )
        )

    if "hotel_need" in uncertain:
        questions.append(
            ClarificationQuestion(
                id="hotel_need",
                header="酒店",
                question="需要一并搜索酒店吗？",
                options=(
                    QuestionOption(
                        label="要酒店",
                        description="将要求酒店日期并搜索酒店",
                        value="hotel:required",
                    ),
                    QuestionOption(
                        label="不要酒店",
                        description="仅交通方案，忽略酒店偏好",
                        value="hotel:skip",
                    ),
                    QuestionOption(
                        label="住一晚",
                        description="按到达日入住、次日退房（需已有到达日）",
                        value="hotel:one_night",
                    ),
                ),
                slots=("hotel_check_in", "hotel_check_out"),
            )
        )

    if "transport_mode" in uncertain:
        questions.append(
            ClarificationQuestion(
                id="transport_mode",
                header="交通",
                question="交通方式目前不明确，请选择硬性要求：",
                options=(
                    QuestionOption(
                        label="只要飞机",
                        description="仅搜索航班",
                        value="mode:flight_only",
                    ),
                    QuestionOption(
                        label="只要高铁/火车",
                        description="仅搜索火车（若供应商无火车库存将无方案）",
                        value="mode:train_only",
                    ),
                    QuestionOption(
                        label="都可以",
                        description="不限制交通模式，由库存与政策筛选",
                        value="mode:any",
                    ),
                ),
                slots=("hard_constraints",),
            )
        )

    from corporate_travel_agent.agent.capability_boundaries import format_capability_conflict

    capability_notes = [
        formatted
        for item in conflicts
        if (formatted := format_capability_conflict(item)) is not None
    ]
    if capability_notes:
        questions.append(
            ClarificationQuestion(
                id="capability",
                header="能力边界",
                question="；".join(capability_notes),
                options=(),
                slots=(),
            )
        )

    # 自由文本冲突说明（无需选项）。
    conflict_lines = [
        f"请修正：{item}"
        for item in conflicts[:5]
        if format_capability_conflict(item) is None
    ]

    if not questions and not conflict_lines:
        return None

    prompt_parts: list[str] = []
    if missing and not questions:
        prompt_parts.append(
            "请补充" + "、".join(_FIELD_LABELS.get(item, item) for item in missing)
        )
    for index, question in enumerate(questions, start=1):
        opt_lines = [
            f"  {chr(ord('A') + i)}. {option.label} — {option.description}"
            for i, option in enumerate(question.options)
        ]
        prompt_parts.append(
            f"{index}. [{question.header}] {question.question}\n" + "\n".join(opt_lines)
        )
    if conflict_lines:
        prompt_parts.extend(conflict_lines)
    prompt_parts.append(
        "请直接回复选项字母/标签，或用自然语言补充信息。"
    )
    prompt_text = "\n".join(prompt_parts)

    return ClarificationBundle(
        questions=tuple(questions),
        missing=missing,
        uncertain=uncertain,
        prompt_text=prompt_text,
    )


def apply_clarification_answer(
    fields: dict[str, Any],
    answer: str,
    *,
    reference_time: Any = None,
    timezone_name: str = "Asia/Shanghai",
    context_message: str | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """把用户选项标签/value 或短指令映射为意图字段更新。

    返回 (updated_fields, applied_notes, remaining_free_text_hints)。
    非已知选项的自由文本留给 LLM 抽取路径。
    ``context_message`` 用于从原始指令推断出行日（如下周三），避免时间模板改成明天。
    """
    from datetime import datetime, time, timedelta
    from zoneinfo import ZoneInfo

    updated = dict(fields)
    notes: list[str] = []
    free_hints: list[str] = []
    raw = (answer or "").strip()
    if not raw:
        return updated, notes, free_hints

    lowered = raw.casefold()
    # 单字母 A/B 由调用方按选项下标处理。
    value = _normalize_option_value(raw)

    def hard() -> list[str]:
        return list(updated.get("hard_constraints") or [])

    def soft() -> list[str]:
        return list(updated.get("soft_preferences") or [])

    def set_hard(name: str, *, remove: tuple[str, ...] = ()) -> None:
        items = [item for item in hard() if item not in remove and item != name]
        items.append(name)
        updated["hard_constraints"] = items

    def drop_hard(*names: str) -> None:
        updated["hard_constraints"] = [item for item in hard() if item not in names]

    def drop_soft(*names: str) -> None:
        updated["soft_preferences"] = [item for item in soft() if item not in names]

    from corporate_travel_agent.services.locations import route_timezones

    origin_tz_name, dest_tz_name = route_timezones(
        updated.get("origin") if isinstance(updated.get("origin"), str) else None,
        updated.get("destination") if isinstance(updated.get("destination"), str) else None,
        fallback=timezone_name,
    )
    origin_tz = ZoneInfo(origin_tz_name)
    dest_tz = ZoneInfo(dest_tz_name)
    # 模板与「明天」锚点在已知出发城时用出发地本地日。
    tz = origin_tz
    now = reference_time
    if not isinstance(now, datetime):
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    if value == "route:Beijing:Shanghai" or "北京→上海" in raw or "北京到上海" in raw:
        updated["origin"] = "Beijing"
        updated["destination"] = "Shanghai"
        notes.append("clarification:route Beijing→Shanghai")
        return updated, notes, free_hints
    if value == "route:Shanghai:Beijing" or "上海→北京" in raw or "上海到北京" in raw:
        updated["origin"] = "Shanghai"
        updated["destination"] = "Beijing"
        notes.append("clarification:route Shanghai→Beijing")
        return updated, notes, free_hints

    iso_range = _ISO_RANGE_RE.fullmatch(value)
    if iso_range is not None:
        from datetime import datetime as dt

        kind = iso_range.group("kind")
        start = dt.fromisoformat(iso_range.group("start"))
        end = dt.fromisoformat(iso_range.group("end"))
        start_tz = dest_tz if kind == "return_window" else origin_tz
        if start.tzinfo is None:
            start = start.replace(tzinfo=start_tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=dest_tz)
        if end <= start:
            free_hints.append("结束时间必须晚于开始时间")
            return updated, notes, free_hints
        if kind == "return_window":
            updated["return_after"] = start
            updated["return_before"] = end
            notes.append(
                f"clarification:return_window={start.isoformat()}/{end.isoformat()}"
            )
        else:
            updated["departure_after"] = start
            updated["arrive_by"] = end
            notes.append(
                f"clarification:window={start.isoformat()}/{end.isoformat()}"
            )
        notes.extend(_sync_hotel_dates_to_trip(updated, dest_tz))
        return updated, notes, free_hints

    date_match = _DATE_OPTION_RE.fullmatch(value)
    if date_match is not None:
        from datetime import date as date_cls

        day = date_cls.fromisoformat(date_match.group(1))
        notes.append(f"clarification:travel_date={day.isoformat()}")
        return updated, notes, free_hints

    city_match = _CITY_OPTION_RE.fullmatch(value)
    if city_match is not None:
        slot = city_match.group(1).casefold()
        canonical = lookup_clarification_city(city_match.group(2))
        if canonical:
            if slot == "city":
                slot = "origin" if not updated.get("origin") else "destination"
            if slot in {"origin", "destination"}:
                other = "destination" if slot == "origin" else "origin"
                if str(updated.get(other) or "").casefold() != canonical.casefold():
                    updated[slot] = canonical
                    notes.append(f"clarification:{slot}={canonical}")
                    return updated, notes, free_hints

    window_match = _WINDOW_OPTION_RE.fullmatch(value)
    if window_match is not None:
        notes.extend(
            _apply_time_window(
                updated,
                depart_hour=int(window_match.group(1)),
                depart_minute=int(window_match.group(2)),
                arrive_hour=int(window_match.group(3)),
                arrive_minute=int(window_match.group(4)),
                origin_tz=origin_tz,
                dest_tz=dest_tz,
                now=now,
                context_message=context_message,
            )
        )
        if notes:
            return updated, notes, free_hints

    clock_match = _CLOCK_OPTION_RE.fullmatch(value)
    if clock_match is not None:
        kind = clock_match.group(1).casefold()
        hour = int(clock_match.group(2))
        minute = int(clock_match.group(3))
        if kind == "arrive":
            notes.extend(
                _apply_clock_arrive_by(
                    updated,
                    f"{hour}:{minute:02d}",
                    dest_tz,
                    origin_tz=origin_tz,
                    now=now,
                    context_message=context_message,
                )
            )
        else:
            notes.extend(
                _apply_clock_depart_after(
                    updated,
                    hour,
                    minute,
                    origin_tz,
                    now=now,
                    context_message=context_message,
                )
            )
        if notes:
            return updated, notes, free_hints

    if value == "return:no" or any(
        token in lowered for token in ("只要去程", "单程", "one-way", "one way", "不要返程")
    ):
        updated["return_after"] = None
        updated["return_before"] = None
        notes.append("clarification:one_way")
        return updated, notes, free_hints

    if value == "return:yes" or any(
        token in lowered for token in ("需要返程", "要返程", "round trip", "往返")
    ):
        notes.append("clarification:return_requested")
        # 时间留空，迫使下一轮澄清或缺槽追问。
        free_hints.append("请补充返程最早出发与最晚到达时间")
        return updated, notes, free_hints

    if value == "return:same_day_afternoon" or "当天下午回" in raw:
        arrive = updated.get("arrive_by")
        if isinstance(arrive, datetime):
            day = arrive.astimezone(dest_tz).date()
        else:
            day = (now.astimezone(dest_tz) + timedelta(days=1)).date()
        updated["return_after"] = datetime.combine(day, time(13, 0), tzinfo=dest_tz)
        updated["return_before"] = datetime.combine(day, time(18, 0), tzinfo=dest_tz)
        notes.append(
            f"clarification:return same_day_afternoon ({dest_tz_name})"
        )
        return updated, notes, free_hints

    if value == "hotel:skip" or value == "hotel:none" or any(
        token in lowered for token in ("不要酒店", "不住酒店", "无需酒店", "no hotel")
    ):
        updated["hotel_check_in"] = None
        updated["hotel_check_out"] = None
        updated["lodging_requirement"] = LodgingRequirement.NOT_REQUIRED.value
        drop_hard("hotel_required")
        drop_soft("hotel_near_client")
        notes.append("clarification:hotel_skip")
        return updated, notes, free_hints

    if value == "hotel:required" or any(
        token in lowered for token in ("要酒店", "需要酒店", "需要住宿")
    ):
        set_hard("hotel_required")
        updated["lodging_requirement"] = LodgingRequirement.REQUIRED.value
        notes.append("clarification:hotel_required")
        free_hints.append("请补充酒店入住与退房日期")
        return updated, notes, free_hints

    if value == "hotel:one_night" or "住一晚" in raw:
        set_hard("hotel_required")
        updated["lodging_requirement"] = LodgingRequirement.REQUIRED.value
        arrive = updated.get("arrive_by")
        dep = updated.get("departure_after")
        if isinstance(arrive, datetime):
            check_in = arrive.astimezone(dest_tz).date()
        elif isinstance(dep, datetime):
            check_in = dep.astimezone(dest_tz).date()
        else:
            check_in = (now.astimezone(dest_tz) + timedelta(days=1)).date()
        check_out = check_in + timedelta(days=1)
        updated["hotel_check_in"] = check_in
        updated["hotel_check_out"] = check_out
        notes.append(f"clarification:hotel_one_night ({dest_tz_name})")
        return updated, notes, free_hints

    if value == "mode:flight_only" or any(
        token in lowered for token in ("只要飞机", "飞机", "flight only", "仅航班")
    ):
        set_hard("flight_only", remove=("train_only",))
        notes.append("clarification:flight_only")
        return updated, notes, free_hints

    if value == "mode:train_only" or any(
        token in lowered for token in ("只要高铁", "只要火车", "train only", "仅火车", "高铁")
    ):
        set_hard("train_only", remove=("flight_only",))
        notes.append("clarification:train_only")
        return updated, notes, free_hints

    if value == "mode:any" or any(
        token in lowered for token in ("都可以", "不限", "any mode")
    ):
        drop_hard("train_only", "flight_only")
        notes.append("clarification:mode_any")
        return updated, notes, free_hints

    if value == "template:day_trip":
        day = (now.astimezone(origin_tz) + timedelta(days=1)).date()
        updated["departure_after"] = datetime.combine(day, time(8, 0), tzinfo=origin_tz)
        updated["arrive_by"] = datetime.combine(day, time(18, 0), tzinfo=dest_tz)
        updated["return_after"] = None
        updated["return_before"] = None
        notes.append(
            f"clarification:template_day_trip (origin {origin_tz_name}, dest {dest_tz_name})"
        )
        notes.extend(_sync_hotel_dates_to_trip(updated, dest_tz))
        return updated, notes, free_hints

    if value == "template:overnight":
        day = (now.astimezone(origin_tz) + timedelta(days=1)).date()
        next_day = day + timedelta(days=1)
        updated["departure_after"] = datetime.combine(day, time(8, 0), tzinfo=origin_tz)
        updated["arrive_by"] = datetime.combine(day, time(18, 0), tzinfo=dest_tz)
        updated["return_after"] = datetime.combine(next_day, time(13, 0), tzinfo=dest_tz)
        updated["return_before"] = datetime.combine(next_day, time(22, 0), tzinfo=dest_tz)
        notes.append(
            f"clarification:template_overnight (origin {origin_tz_name}, dest {dest_tz_name})"
        )
        notes.extend(_sync_hotel_dates_to_trip(updated, dest_tz))
        return updated, notes, free_hints

    if value == "client:skip" or any(
        token in lowered for token in ("先按目的地", "先按城市", "不按通勤")
    ):
        updated["client_location"] = "SKIPPED"
        notes.append("clarification:client_location_skipped")
        return updated, notes, free_hints

    if value == "free_text" or lowered in {"自己说明", "自己写时间", "自己写日期"}:
        free_hints.append("请用自然语言补充具体信息")
        return updated, notes, free_hints

    clock_notes = _apply_clock_arrive_by(
        updated,
        raw,
        dest_tz,
        origin_tz=origin_tz,
        now=now,
        context_message=context_message,
    )
    if clock_notes:
        notes.extend(clock_notes)
        return updated, notes, free_hints

    # 无题目上下文时不单独应用 A/B/C/D。
    free_hints.append(raw)
    return updated, notes, free_hints


def should_defer_structured_option_to_llm(message: str, notes: list[str]) -> bool:
    """结构化捷径若只是长句子串则交给 LLM，避免截断语义。

    UI 精确机值（如 ``template:overnight``）须立即生效。长度启发式避免
    「北京到上海，8月5日…」被收成路线捷径而丢掉后半句。
    """
    if not notes:
        return False
    compact = (message or "").strip()
    if is_exact_option_value(compact):
        return False
    return any(mark in compact for mark in "，。；、,;.") or len(compact) > 16


def _inferred_trip_date(
    fields: dict[str, Any],
    tz: Any,
    now: Any,
    context_message: str | None,
):
    """出行日：已有时间槽 > 原始指令里的星期/日期 > 明天。"""
    from datetime import datetime, timedelta

    from corporate_travel_agent.agent.local_intent import GroundedLocalIntentParser

    for key in ("departure_after", "arrive_by", "return_after"):
        value = fields.get(key)
        if isinstance(value, datetime):
            return value.astimezone(tz).date() if value.tzinfo else value.date()
    if context_message:
        day = GroundedLocalIntentParser()._outbound_date(context_message, now)
        if day is not None:
            return day
    return (now.astimezone(tz) + timedelta(days=1)).date()


def _apply_clock_depart_after(
    fields: dict[str, Any],
    hour: int,
    minute: int,
    origin_tz: Any,
    *,
    now: Any,
    context_message: str | None,
) -> list[str]:
    """把出发钟点落到已识别出行日。"""
    from datetime import datetime, time

    if hour > 23 or hour < 0 or minute > 59:
        return []
    day = _inferred_trip_date(fields, origin_tz, now, context_message)
    fields["departure_after"] = datetime.combine(day, time(hour, minute), tzinfo=origin_tz)
    return [f"clarification:departure_after={fields['departure_after'].isoformat()}"]


def _apply_time_window(
    fields: dict[str, Any],
    *,
    depart_hour: int,
    depart_minute: int,
    arrive_hour: int,
    arrive_minute: int,
    origin_tz: Any,
    dest_tz: Any,
    now: Any,
    context_message: str | None,
) -> list[str]:
    """一次点选同时写下出发和到达窗口。"""
    from datetime import datetime, time

    day = _inferred_trip_date(fields, origin_tz, now, context_message)
    fields["departure_after"] = datetime.combine(
        day, time(depart_hour, depart_minute), tzinfo=origin_tz
    )
    fields["arrive_by"] = datetime.combine(
        day, time(arrive_hour, arrive_minute), tzinfo=dest_tz
    )
    notes = [
        f"clarification:departure_after={fields['departure_after'].isoformat()}",
        f"clarification:arrive_by={fields['arrive_by'].isoformat()}",
    ]
    notes.extend(_sync_hotel_dates_to_trip(fields, dest_tz))
    return notes


def _apply_clock_arrive_by(
    fields: dict[str, Any],
    raw: str,
    dest_tz: Any,
    *,
    origin_tz: Any = None,
    now: Any = None,
    context_message: str | None = None,
) -> list[str]:
    """把「晚上九点」或 ``18:00`` 补到已知出行日的 arrive_by。"""
    from datetime import datetime, time

    text = (raw or "").strip()
    hour = -1
    minute = 0
    match = _CLOCK_ANSWER_RE.match(text)
    clock_option = _CLOCK_OPTION_RE.fullmatch(text) or re.fullmatch(
        r"(\d{1,2}):(\d{2})", text
    )
    if match is not None:
        if match.group("hour"):
            hour = int(match.group("hour"))
        else:
            hour = _CN_HOUR.get(match.group("cn") or "", -1)
        minute = int(match.group("minute") or 0)
        period = (match.group("period") or "").strip()
        if period in {"下午", "傍晚", "晚上", "今晚"} and 1 <= hour <= 11:
            hour += 12
        elif period == "中午" and hour in {0, 12}:
            hour = 12
    elif clock_option is not None:
        if clock_option.lastindex and clock_option.lastindex >= 2:
            option_text = clock_option.group(0)
            if option_text.startswith("arrive:") or option_text.startswith("depart:"):
                hour = int(clock_option.group(2))
                minute = int(clock_option.group(3))
            else:
                hour = int(clock_option.group(1))
                minute = int(clock_option.group(2))
    if hour > 23 or hour < 0 or minute > 59:
        return []
    if fields.get("arrive_by") is not None:
        return []
    anchor_tz = dest_tz
    day = _inferred_trip_date(fields, dest_tz, now or datetime.now(dest_tz), context_message)
    fields["arrive_by"] = datetime.combine(day, time(hour, minute), tzinfo=anchor_tz)
    notes = [f"clarification:arrive_by={fields['arrive_by'].isoformat()}"]
    if fields.get("departure_after") is None and origin_tz is not None:
        fields["departure_after"] = datetime.combine(day, time(8, 0), tzinfo=origin_tz)
        notes.append(
            f"clarification:departure_after={fields['departure_after'].isoformat()}"
        )
    return notes


def _sync_hotel_dates_to_trip(fields: dict[str, Any], dest_tz: Any) -> list[str]:
    """时间模板后，使酒店入住/退房与行程锚点对齐。"""
    from datetime import datetime, timedelta

    lodging_required = (
        fields.get("lodging_requirement") == LodgingRequirement.REQUIRED.value
        or "hotel_required" in (fields.get("hard_constraints") or ())
        or fields.get("hotel_check_in") is not None
        or fields.get("hotel_check_out") is not None
    )
    if not lodging_required:
        return []

    arrive = fields.get("arrive_by")
    ret = fields.get("return_after")
    notes: list[str] = []
    if isinstance(arrive, datetime):
        check_in = arrive.astimezone(dest_tz).date() if arrive.tzinfo else arrive.date()
        if fields.get("hotel_check_in") != check_in:
            fields["hotel_check_in"] = check_in
            notes.append(f"clarification:hotel_check_in={check_in.isoformat()}")
    check_in = fields.get("hotel_check_in")
    if isinstance(ret, datetime):
        check_out = ret.astimezone(dest_tz).date() if ret.tzinfo else ret.date()
        if fields.get("hotel_check_out") != check_out:
            fields["hotel_check_out"] = check_out
            notes.append(f"clarification:hotel_check_out={check_out.isoformat()}")
    elif check_in is not None and fields.get("hotel_check_out") is None:
        fields["hotel_check_out"] = check_in + timedelta(days=1)
        notes.append(
            f"clarification:hotel_check_out={fields['hotel_check_out'].isoformat()}"
        )
    return notes


def apply_option_letter(
    fields: dict[str, Any],
    letter: str,
    questions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    reference_time: Any = None,
    timezone_name: str = "Asia/Shanghai",
    context_message: str | None = None,
) -> tuple[dict[str, Any], list[str], list[str]] | None:
    """若用户答 A/B/C，映射为第一题对应选项的 value。"""
    letter = (letter or "").strip().upper()
    if len(letter) != 1 or letter < "A" or letter > "Z":
        return None
    if not questions:
        return None
    options = questions[0].get("options") or []
    index = ord(letter) - ord("A")
    if index < 0 or index >= len(options):
        return None
    value = options[index].get("value") or options[index].get("label") or ""
    return apply_clarification_answer(
        fields,
        str(value),
        reference_time=reference_time,
        timezone_name=timezone_name,
        context_message=context_message,
    )


def _normalize_option_value(raw: str) -> str:
    """从回答中抽出已知选项机值或清洗后的文本。"""
    text = raw.strip()
    # 允许「A. 只要去程」写法。
    if len(text) >= 2 and text[0].upper() in "ABCD" and text[1] in ".)、. ":
        # 完整标签走下方自由解析；单字母他处处理。
        rest = text[2:].strip()
        if rest:
            text = rest
    # 回答中嵌入的已知机值。
    for token in KNOWN_OPTION_VALUES:
        if token in text:
            return token
    return text
