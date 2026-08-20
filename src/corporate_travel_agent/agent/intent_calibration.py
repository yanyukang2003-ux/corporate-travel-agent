"""intent_calibration：P0 安全默认 + P1 SearchReady 业务校准（防编造）。

设计目标：
- 优先确定性、可审计的默认，而非模型编造。
- 模型修复只能填*缺失*槽，且须有用户文本依据。
- 从不静默编造城市、出行日或用户证据不足的约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from corporate_travel_agent.domain.enums import LodgingRequirement
from corporate_travel_agent.services.locations import route_timezones

# 白名单默认——仅这些可在无模型重抽时填充。
DEFAULT_DEPARTURE_HOUR = 8
DEFAULT_ARRIVE_BY_HOUR = 18
DEFAULT_RETURN_AFTER_HOUR = 13
DEFAULT_RETURN_BEFORE_HOUR = 18
MAX_INTENT_REPAIR_ATTEMPTS = 2

_SLOT_FIELDS = (
    "origin",
    "destination",
    "departure_after",
    "arrive_by",
    "return_after",
    "return_before",
    "hotel_check_in",
    "hotel_check_out",
    "client_location",
    "hard_constraints",
    "soft_preferences",
)

_HOTEL_REQUEST_RE = re.compile(
    r"(hotel|lodging|accommodation|check[ -]?in|check[ -]?out|"
    r"酒店|住宿|入住|退房|住一晚|住两晚|订一晚|订两晚|"
    r"two[- ]nights?|one[- ]night)",
    re.I,
)
_HOTEL_NOT_REQUIRED_RE = re.compile(
    r"(不需要|无需|不要|不用|不住|不订|不预订|取消).{0,12}(酒店|住宿)|"
    r"(酒店|住宿).{0,4}(不需要|无需|不用|取消|自理|自行安排|不要了)|"
    r"(已有|已经有|已安排).{0,8}(酒店|住宿)|"
    r"住宿自理|酒店自理|自行安排住宿|"
    r"\b(no|without)\s+(hotel|lodging|accommodation)\b|"
    r"\b(do not|don't|do not need|don't need)\b.{0,12}\b(hotel|lodging|accommodation)\b|"
    r"\b(hotel|lodging|accommodation)\b.{0,12}\b(self[- ]arranged|not required)\b",
    re.I,
)
_HOTEL_CONDITIONAL_RE = re.compile(
    r"(如果|如|若|假如).{0,12}(需要|要|再订|再安排|订).{0,8}(酒店|住宿)|"
    r"(如果|若|假如).{0,8}回不来|"
    r"回不来.{0,6}(再)?(订|安排)(酒店|住宿)|"
    r"(酒店|住宿).{0,8}(如果需要|如有需要|视情况)|"
    r"\b(if|when)\b.{0,16}\b(hotel|lodging|accommodation)\b.{0,12}\b(needed|required)\b|"
    r"\bif needed\b.{0,16}\b(hotel|lodging|accommodation)\b|"
    r"\b(hotel|lodging|accommodation)\b.{0,12}\b(if needed|if required)\b|"
    r"\bif\b.{0,28}\b(can'?t|cannot|don't)\b.{0,20}\b(return|make it back|get back)\b.{0,24}"
    r"\b(hotel|lodging|accommodation)\b",
    re.I,
)
_EXPLICIT_YEAR_RE = re.compile(r"20\d{2}|明年|次年|下一年|next year", re.I)
# 无年份的「刚过去」月日是用户错误，不是「明年」。
MAX_RECENT_PAST_ROLL_DAYS = 120
_COMPARE_TRANSPORT_RE = re.compile(
    r"("
    r"compare.{0,24}(train|flight|高铁|飞机)|"
    r"(train|高铁).{0,16}(and|or|和|与|还是).{0,16}(flight|飞机)|"
    r"(flight|飞机).{0,16}(and|or|和|与|还是).{0,16}(train|高铁)|"
    r"对比.{0,12}(高铁|火车|飞机|航班)|"
    r"(高铁|火车).{0,8}对比.{0,8}(飞机|航班)|"
    r"(飞机|航班).{0,8}对比.{0,8}(高铁|火车)"
    r")",
    re.I,
)
_CITY_ALTERNATIVE_RE = re.compile(
    r"或者|还是|或是|要么|(?<![a-z])or(?![a-z])|either",
    re.I,
)
_ROUTE_HINT_RE = re.compile(
    r"从.+?(?:去|到|至)|from\s+.+\s+to\s+",
    re.I,
)
_WEEKDAY_CN_RE = re.compile(r"(下下|下|本|这)?周[一二三四五六日天]")
_CALENDAR_DATE_RE = re.compile(
    r"(?P<iso>20\d{2}-\d{2}-\d{2})"
    r"|(?P<ymd>20\d{2}[./]\d{1,2}[./]\d{1,2})"
    r"|(?P<cn>\d{1,2}月\d{1,2}[号日]?)"
    r"|(?P<num>\d{1,2}[./]\d{1,2}(?!\d)(?!\s*(?:小时|折|成|km|公里)))"
)
_LUNAR_HOLIDAY_RE = re.compile(
    r"农历|阴历|正月|腊月|初[一二三四五六七八九十]+|"
    r"春节|除夕|元宵|清明|端午|中秋|国庆|元旦|"
    r"劳动节|圣诞|\blunar\b|spring festival|chinese new year",
    re.I,
)
_DATE_SLOT_NAMES = (
    "departure_after",
    "arrive_by",
    "return_after",
    "return_before",
    "hotel_check_in",
    "hotel_check_out",
)
_MEETING_REVISION_RE = re.compile(
    r"(会议|开会|\bmeeting\b).{0,16}(改到|改成|改为|到)",
    re.I,
)
_CLOCK_FRAGMENT_RE = re.compile(
    r"(?P<period>早上|上午|中午|下午|傍晚|晚上)?"
    r"\s*(?P<hour>\d{1,2})\s*(?:点|時|时|:|：)\s*(?P<minute>\d{1,2})?",
)
_RETURN_REVISION_RE = re.compile(
    r"(返程|回程|返回).{0,12}(改|到)|改.{0,8}(返程|回程)|"
    r"(?:change|move).{0,16}\breturn\b",
    re.I,
)
_RETURN_SCOPED_RELATIVE_DAY_RE = re.compile(
    r"(?:返程|回程|返回).{0,8}(?:后天|明天)|(?:后天|明天).{0,8}(?:返程|回程|返回)"
)
_TWO_NIGHTS_RE = re.compile(
    r"住两晚|订两晚|两晚酒店|two[- ]nights?|\b2\s+nights?\b",
    re.I,
)
_ONE_NIGHT_RE = re.compile(
    r"住一晚|订一晚|一晚住宿|one[- ]night|\bone night\b",
    re.I,
)


def message_requests_hotel(user_message: str) -> bool:
    """当前用户文本是否明确落实了酒店搜索需求。"""
    return bool(_HOTEL_REQUEST_RE.search(user_message or ""))


def resolve_lodging_requirement(
    fields: dict[str, Any],
    *,
    user_message: str,
) -> LodgingRequirement:
    """解析明确住宿意图，不从行程跨度推断。

    显式否定与条件句优先于泛化酒店关键词。未提住宿的跟进轮次保留已落地状态。
    软性通勤偏好仅在本轮确实提到住宿时才升级。
    """
    message = user_message or ""
    if _HOTEL_NOT_REQUIRED_RE.search(message):
        return LodgingRequirement.NOT_REQUIRED
    if _HOTEL_CONDITIONAL_RE.search(message):
        return LodgingRequirement.UNSPECIFIED
    if message_requests_hotel(message):
        return LodgingRequirement.REQUIRED

    current_raw = fields.get("lodging_requirement")
    try:
        current = LodgingRequirement(str(current_raw))
    except ValueError:
        current = LodgingRequirement.UNSPECIFIED
    if current is LodgingRequirement.NOT_REQUIRED:
        return current
    if (
        "hotel_required" in (fields.get("hard_constraints") or ())
        or fields.get("hotel_check_in") is not None
        or fields.get("hotel_check_out") is not None
    ):
        return LodgingRequirement.REQUIRED
    return current


@dataclass(frozen=True, slots=True)
class SearchReadyResult:
    """库存搜索就绪检查结果（缺失/冲突/备注）。"""

    ready: bool
    missing: tuple[str, ...]
    conflicts: tuple[str, ...]
    notes: tuple[str, ...] = ()


@dataclass
class CalibrationTrace:
    """一次抽取→校准周期的审计轨迹（可含修复）。"""

    initial_missing: tuple[str, ...] = ()
    defaults_applied: list[str] = field(default_factory=list)
    repair_attempts: int = 0
    repair_missing_before: list[tuple[str, ...]] = field(default_factory=list)
    repair_rejected_fields: list[str] = field(default_factory=list)
    field_provenance: dict[str, str] = field(default_factory=dict)
    final_missing: tuple[str, ...] = ()
    ready: bool = False
    # Claude Code 风格参数环审计（L1 Schema → L2 业务 → tool_use_error）。
    param_loop: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "initial_missing": list(self.initial_missing),
            "defaults_applied": list(self.defaults_applied),
            "repair_attempts": self.repair_attempts,
            "repair_missing_before": [list(item) for item in self.repair_missing_before],
            "repair_rejected_fields": list(self.repair_rejected_fields),
            "field_provenance": dict(self.field_provenance),
            "final_missing": list(self.final_missing),
            "ready": self.ready,
            "param_loop": dict(self.param_loop) if self.param_loop else {},
        }


def search_ready_missing(
    fields: dict[str, Any],
    *,
    classification: str,
) -> SearchReadyResult:
    """库存搜索的业务完备性（严于裸 Schema 形状）。"""
    if classification == "OUT_OF_SCOPE":
        return SearchReadyResult(ready=True, missing=(), conflicts=())

    missing: list[str] = []
    notes: list[str] = []

    for name in ("origin", "destination", "departure_after", "arrive_by"):
        if fields.get(name) in (None, ""):
            missing.append(name)

    hard = list(fields.get("hard_constraints") or ())
    soft = list(fields.get("soft_preferences") or ())
    has_partial_return = (fields.get("return_after") is None) != (
        fields.get("return_before") is None
    )
    if has_partial_return:
        if fields.get("return_after") is None:
            missing.append("return_after")
        if fields.get("return_before") is None:
            missing.append("return_before")

    hotel_required = (
        fields.get("lodging_requirement") == LodgingRequirement.REQUIRED.value
        or "hotel_required" in hard
    )
    if hotel_required:
        if fields.get("hotel_check_in") is None:
            missing.append("hotel_check_in")
        if fields.get("hotel_check_out") is None:
            missing.append("hotel_check_out")

    # Proximity alone is not enough when no user message grounded the hotel request.
    if "hotel_near_client" in soft and not hotel_required:
        notes.append("hotel_near_client is ungrounded while lodging remains UNSPECIFIED")
    if "hotel_near_client" in soft and hotel_required and not fields.get("client_location"):
        notes.append(
            "hotel_near_client requested without client_location; "
            "destination-city hotels will be quoted and commute stays unknown"
        )

    missing = list(dict.fromkeys(missing))
    return SearchReadyResult(
        ready=not missing,
        missing=tuple(missing),
        conflicts=(),
        notes=tuple(notes),
    )


def apply_safe_defaults(
    fields: dict[str, Any],
    *,
    classification: str,
    reference_time: datetime,
    timezone_name: str,
    user_message: str,
    provenance: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """在用户文本支撑行程形状时，仅填充白名单默认。

    防编造规则：
    - 从不编造出发/目的城市。
    - 时间仅从已抽取日期与有依据意图推导。
    - 返程默认可补全已落地的半对，绝不凭空造一对。
    - 每次填充标记 policy_default 并写入 assumptions。
    - 本地钟点默认优先用出发/目的地 IANA 时区，而非仅政策家乡时区。
    """
    updated = dict(fields)
    assumptions: list[str] = []
    prov = dict(provenance or {})
    updated, alt_notes = scrub_alternative_city_pair(updated, user_message=user_message)
    assumptions.extend(alt_notes)
    if alt_notes:
        prov.pop("origin", None)
        prov.pop("destination", None)
    updated, weekday_notes, prov = scrub_ambiguous_weekday_choice(
        updated, user_message=user_message, provenance=prov
    )
    assumptions.extend(weekday_notes)
    updated, holiday_notes, prov = scrub_ungrounded_holiday_dates(
        updated, user_message=user_message, provenance=prov
    )
    assumptions.extend(holiday_notes)
    origin_tz_name, dest_tz_name = route_timezones(
        updated.get("origin") if isinstance(updated.get("origin"), str) else None,
        updated.get("destination") if isinstance(updated.get("destination"), str) else None,
        fallback=timezone_name,
    )
    origin_tz = ZoneInfo(origin_tz_name)
    dest_tz = ZoneInfo(dest_tz_name)
    policy_tz = ZoneInfo(timezone_name)
    # 多日/酒店日历锚点在已知时跟随出发地本地日。
    anchor_tz = origin_tz
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=anchor_tz)
    else:
        reference_time = reference_time.astimezone(anchor_tz)

    hard = list(updated.get("hard_constraints") or ())
    _ = classification

    # --- 明确住宿三态（从不由行程跨度推断） ---
    previous_lodging = updated.get("lodging_requirement")
    lodging_requirement = resolve_lodging_requirement(
        updated,
        user_message=user_message,
    )
    updated["lodging_requirement"] = lodging_requirement.value
    if lodging_requirement is LodgingRequirement.REQUIRED:
        if "hotel_required" not in hard:
            hard.append("hotel_required")
        updated["hard_constraints"] = hard
    elif lodging_requirement is LodgingRequirement.NOT_REQUIRED:
        hard = [item for item in hard if item != "hotel_required"]
        updated["hard_constraints"] = hard
        updated["hotel_check_in"] = None
        updated["hotel_check_out"] = None
        prov.pop("hotel_check_in", None)
        prov.pop("hotel_check_out", None)
    elif _HOTEL_CONDITIONAL_RE.search(user_message or ""):
        hard = [item for item in hard if item != "hotel_required"]
        updated["hard_constraints"] = hard
        updated["hotel_check_in"] = None
        updated["hotel_check_out"] = None
        prov.pop("hotel_check_in", None)
        prov.pop("hotel_check_out", None)
    if previous_lodging != lodging_requirement.value:
        assumptions.append(f"policy_default:lodging_requirement={lodging_requirement.value}")
        prov["lodging_requirement"] = "policy_default"
    updated, mode_notes = reconcile_transport_mode_compare(updated, user_message=user_message)
    assumptions.extend(mode_notes)
    if (
        lodging_requirement is LodgingRequirement.REQUIRED
        and "hotel_near_client" in (updated.get("soft_preferences") or ())
        and not updated.get("client_location")
    ):
        assumptions.append(
            "hotel_near_client requested without client_location; "
            "hotels are destination-city quotes and commute stays unknown"
        )

    # --- 纠正误盖政策时区的类默认本地时间窗 ---
    # 例：NY→PHL 却用 Asia/Shanghai 的 08:00/18:00。
    updated, rebase_assumptions, prov = _rebase_default_local_times(
        updated,
        provenance=prov,
        origin_tz=origin_tz,
        dest_tz=dest_tz,
        policy_tz_name=timezone_name,
        origin_tz_name=origin_tz_name,
        dest_tz_name=dest_tz_name,
    )
    assumptions.extend(rebase_assumptions)

    # --- 由已抽取 arrive_by 推导 departure_after（同本地日 08:00） ---
    # 安全：arrive_by 来自用户文本；仅补保守早晨下界。
    if updated.get("departure_after") is None and isinstance(updated.get("arrive_by"), datetime):
        arrive = updated["arrive_by"]
        if arrive.tzinfo is None:
            arrive = arrive.replace(tzinfo=dest_tz)
        else:
            arrive = arrive.astimezone(dest_tz)
        # Departure morning is origin-local on the destination local calendar day.
        dep = datetime.combine(
            arrive.date(),
            time(DEFAULT_DEPARTURE_HOUR, 0),
            tzinfo=origin_tz,
        )
        if dep < arrive:
            updated["departure_after"] = dep
            assumptions.append(
                f"policy_default:departure_after={dep.isoformat()} "
                f"(origin-local {origin_tz_name} {DEFAULT_DEPARTURE_HOUR:02d}:00 "
                f"same calendar day as arrive_by; derived, not invented city/day)"
            )
            prov["departure_after"] = "policy_default"

    # --- 补全返程成对（校验要求成对或皆空） ---
    # 返程离开目的地，用目的地本地钟。
    ret_after = updated.get("return_after")
    ret_before = updated.get("return_before")
    return_previously_grounded = any(
        updated.get(name) is not None
        and prov.get(name) in {"model_extract", "model_repair"}
        for name in ("return_after", "return_before")
    )
    if (
        (ret_after is None) != (ret_before is None)
        and (_message_suggests_return(user_message) or return_previously_grounded)
        and not _message_explicitly_one_way(user_message)
    ):
        anchor = _return_anchor_date(updated, reference_time, dest_tz)
        if anchor is not None:
            if updated.get("return_after") is None:
                ra = datetime.combine(
                    anchor, time(DEFAULT_RETURN_AFTER_HOUR, 0), tzinfo=dest_tz
                )
                # Keep ordering if only return_before exists.
                if isinstance(updated.get("return_before"), datetime):
                    rb = updated["return_before"]
                    rb = rb.astimezone(dest_tz) if rb.tzinfo else rb.replace(tzinfo=dest_tz)
                    if ra >= rb:
                        ra = rb - timedelta(hours=1)
                updated["return_after"] = ra
                assumptions.append(
                    f"policy_default:return_after={ra.isoformat()} "
                    f"(destination-local {dest_tz_name} pair completion / afternoon window "
                    f"{DEFAULT_RETURN_AFTER_HOUR:02d}:00)"
                )
                prov["return_after"] = "policy_default"
            if updated.get("return_before") is None:
                rb = datetime.combine(
                    anchor, time(DEFAULT_RETURN_BEFORE_HOUR, 0), tzinfo=dest_tz
                )
                if isinstance(updated.get("return_after"), datetime):
                    ra = updated["return_after"]
                    ra = ra.astimezone(dest_tz) if ra.tzinfo else ra.replace(tzinfo=dest_tz)
                    if rb <= ra:
                        rb = ra + timedelta(hours=1)
                updated["return_before"] = rb
                assumptions.append(
                    f"policy_default:return_before={rb.isoformat()} "
                    f"(destination-local {dest_tz_name} pair completion / afternoon window "
                    f"{DEFAULT_RETURN_BEFORE_HOUR:02d}:00)"
                )
                prov["return_before"] = "policy_default"

    # --- 由行程锚点补全酒店成对（从不编造城市） ---
    h_in = updated.get("hotel_check_in")
    h_out = updated.get("hotel_check_out")
    needs_hotel = lodging_requirement is LodgingRequirement.REQUIRED
    if needs_hotel and (h_in is None or h_out is None):
        arrival_anchor = updated.get("arrive_by") or updated.get("departure_after")
        if h_in is None and isinstance(arrival_anchor, datetime):
            d = arrival_anchor
            d = d.astimezone(dest_tz) if d.tzinfo else d.replace(tzinfo=dest_tz)
            candidate_check_in = d.date()
            if not isinstance(h_out, date) or candidate_check_in < h_out:
                updated["hotel_check_in"] = candidate_check_in
                assumptions.append(
                    f"policy_default:hotel_check_in={candidate_check_in.isoformat()} "
                    "(from arrive_by, destination-local calendar day)"
                )
                prov["hotel_check_in"] = "policy_default"
                h_in = candidate_check_in
        if h_out is None:
            if isinstance(updated.get("return_after"), datetime):
                r = updated["return_after"]
                r = r.astimezone(dest_tz) if r.tzinfo else r.replace(tzinfo=dest_tz)
                updated["hotel_check_out"] = r.date()
                assumptions.append(
                    f"policy_default:hotel_check_out={updated['hotel_check_out'].isoformat()} "
                    "(from return_after)"
                )
                prov["hotel_check_out"] = "policy_default"
            elif isinstance(h_in, date) and _message_requested_hotel_nights(user_message) == 1:
                updated["hotel_check_out"] = h_in + timedelta(days=1)
                assumptions.append(
                    f"policy_default:hotel_check_out={updated['hotel_check_out'].isoformat()} "
                    "(one night after check-in; pair completion)"
                )
                prov["hotel_check_out"] = "policy_default"
    # policy_tz retained so call sites that only pass timezone_name stay valid.
    _ = policy_tz
    updated, year_notes, prov = scrub_unconfirmed_next_year_dates(
        updated,
        user_message=user_message,
        reference_time=reference_time,
        provenance=prov,
    )
    assumptions.extend(year_notes)
    updated, followup_notes, prov = apply_followup_slot_revisions(
        updated,
        user_message=user_message,
        reference_time=reference_time,
        timezone_name=timezone_name,
        provenance=prov,
    )
    assumptions.extend(followup_notes)
    return updated, assumptions, prov


def scrub_alternative_city_pair(
    fields: dict[str, Any],
    *,
    user_message: str,
) -> tuple[dict[str, Any], list[str]]:
    """用户把两城当二选一时，丢弃编造的 OD 对。"""
    updated = dict(fields)
    origin = updated.get("origin")
    dest = updated.get("destination")
    if not origin or not dest:
        return updated, []
    if _ROUTE_HINT_RE.search(user_message or ""):
        return updated, []
    if not _CITY_ALTERNATIVE_RE.search(user_message or ""):
        return updated, []
    if not (_city_appears_in_message(user_message, str(origin)) and _city_appears_in_message(
        user_message, str(dest)
    )):
        return updated, []
    updated["origin"] = None
    updated["destination"] = None
    return updated, ["rejected_alternative_cities:origin_destination"]


def iter_calendar_date_mentions(message: str) -> list[tuple[int | None, int, int]]:
    """按消息顺序解析公历日期：(年或 None, 月, 日)。"""
    found: list[tuple[int | None, int, int]] = []
    for match in _CALENDAR_DATE_RE.finditer(message or ""):
        parsed = _parse_calendar_match(match)
        if parsed is not None:
            found.append(parsed)
    return found


def message_has_gregorian_date(message: str) -> bool:
    """消息是否含公历日期。"""
    return bool(iter_calendar_date_mentions(message))


def message_has_ambiguous_weekday_choice(message: str) -> bool:
    """是否出现「这周五还是下周五」类歧义。"""
    hits = list(_WEEKDAY_CN_RE.finditer(message or ""))
    if len(hits) < 2:
        return False
    between = (message or "")[hits[0].start() : hits[1].end()]
    return bool(_CITY_ALTERNATIVE_RE.search(between))


def message_is_lunar_or_holiday_without_gregorian(message: str) -> bool:
    """有农历/节假日表述但无公历日期。"""
    return bool(_LUNAR_HOLIDAY_RE.search(message or "")) and not message_has_gregorian_date(
        message
    )


def resolve_calendar_mention(
    mention: tuple[int | None, int, int],
    *,
    reference_time: datetime,
    min_day: date | None = None,
) -> date | None:
    """相对时钟或去程下限，解析 (年,月,日)。"""
    year, month, day = mention
    try:
        if year is not None:
            return date(year, month, day)
        if min_day is not None:
            candidate = date(min_day.year, month, day)
            if candidate >= min_day:
                return candidate
            return date(min_day.year + 1, month, day)
        candidate = date(reference_time.year, month, day)
    except ValueError:
        return None
    if candidate >= reference_time.date():
        return candidate
    age_days = (reference_time.date() - candidate).days
    if age_days <= MAX_RECENT_PAST_ROLL_DAYS:
        return None
    try:
        return date(reference_time.year + 1, month, day)
    except ValueError:
        return None


def _parse_calendar_match(match: re.Match[str]) -> tuple[int | None, int, int] | None:
    raw_iso = match.group("iso")
    if raw_iso:
        parsed = date.fromisoformat(raw_iso)
        return parsed.year, parsed.month, parsed.day
    raw_ymd = match.group("ymd")
    if raw_ymd:
        parts = re.split(r"[./]", raw_ymd)
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        return _validated_ymd(year, month, day)
    raw_cn = match.group("cn")
    if raw_cn:
        numbers = re.findall(r"\d+", raw_cn)
        if len(numbers) != 2:
            return None
        return _validated_ymd(None, int(numbers[0]), int(numbers[1]))
    raw_num = match.group("num")
    if raw_num:
        parts = re.split(r"[./]", raw_num)
        return _validated_ymd(None, int(parts[0]), int(parts[1]))
    return None


def _validated_ymd(
    year: int | None, month: int, day: int
) -> tuple[int | None, int, int] | None:
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None
    probe_year = year if year is not None else 2024
    try:
        date(probe_year, month, day)
    except ValueError:
        return None
    return year, month, day


def _clear_date_slots(
    fields: dict[str, Any], provenance: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """清空日期相关槽位与出处。"""
    updated = dict(fields)
    prov = dict(provenance)
    for name in _DATE_SLOT_NAMES:
        updated[name] = None
        prov.pop(name, None)
    return updated, prov


def scrub_ambiguous_weekday_choice(
    fields: dict[str, Any],
    *,
    user_message: str,
    provenance: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """「这周五还是下周五」不是公历日证据。"""
    prov = dict(provenance or {})
    if not message_has_ambiguous_weekday_choice(user_message):
        return dict(fields), [], prov
    if message_has_gregorian_date(user_message):
        return dict(fields), [], prov
    updated, prov = _clear_date_slots(fields, prov)
    return updated, ["rejected_ambiguous_weekday_choice"], prov


def scrub_ungrounded_holiday_dates(
    fields: dict[str, Any],
    *,
    user_message: str,
    provenance: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """农历/节假日名本身不是公历日，除非用户另给公历。"""
    prov = dict(provenance or {})
    if not message_is_lunar_or_holiday_without_gregorian(user_message):
        return dict(fields), [], prov
    updated, prov = _clear_date_slots(fields, prov)
    return updated, ["rejected_ungrounded_lunar_or_holiday"], prov


def _city_appears_in_message(message: str, city: str) -> bool:
    """城市或其别名是否出现在消息中。"""
    text = (message or "").casefold()
    if city.casefold() in text:
        return True
    extras = {
        "Beijing": ("北京", "beijing", "帝都"),
        "Shanghai": ("上海", "shanghai", "魔都"),
        "London": ("伦敦", "倫敦", "london"),
        "New York": ("纽约", "紐約", "new york", "nyc"),
        "Hong Kong": ("香港", "hong kong"),
        "Tokyo": ("东京", "東京", "tokyo"),
        "Tianjin": ("天津", "tianjin"),
    }
    return any(token.casefold() in text for token in extras.get(city, ()))


def reconcile_transport_mode_compare(
    fields: dict[str, Any],
    *,
    user_message: str,
) -> tuple[dict[str, Any], list[str]]:
    """对比需求会清掉残留的 train_only/flight_only 硬约束。"""
    updated = dict(fields)
    soft = list(updated.get("soft_preferences") or [])
    hard = list(updated.get("hard_constraints") or [])
    compare = "compare_train_and_flight" in soft or bool(
        _COMPARE_TRANSPORT_RE.search(user_message or "")
    )
    if not compare:
        return updated, []
    next_hard = [item for item in hard if item not in {"train_only", "flight_only"}]
    if "compare_train_and_flight" not in soft:
        soft.append("compare_train_and_flight")
    updated["hard_constraints"] = next_hard
    updated["soft_preferences"] = soft
    if next_hard != hard or "compare_train_and_flight" not in (
        fields.get("soft_preferences") or ()
    ):
        return updated, ["policy_default:compare_clears_exclusive_mode"]
    return updated, []


def scrub_unconfirmed_next_year_dates(
    fields: dict[str, Any],
    *,
    user_message: str,
    reference_time: datetime,
    provenance: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """丢弃由「无年份且刚过去」月日编造出的明年日期。

    2026-08-19 说「8月5日」不是 2027-08-05 的证据，除非用户点明年/年份。
    """
    updated = dict(fields)
    notes: list[str] = []
    prov = dict(provenance or {})
    if _EXPLICIT_YEAR_RE.search(user_message or ""):
        return updated, notes, prov
    ref_date = (
        reference_time.date()
        if isinstance(reference_time, datetime)
        else reference_time
    )
    names = (
        "departure_after",
        "arrive_by",
        "return_after",
        "return_before",
        "hotel_check_in",
        "hotel_check_out",
    )
    for name in names:
        value = updated.get(name)
        if isinstance(value, datetime):
            value_date = value.date()
        elif isinstance(value, date):
            value_date = value
        else:
            continue
        if value_date.year != ref_date.year + 1:
            continue
        try:
            this_year = date(ref_date.year, value_date.month, value_date.day)
        except ValueError:
            continue
        if this_year >= ref_date:
            continue
        if (ref_date - this_year).days > MAX_RECENT_PAST_ROLL_DAYS:
            continue
        updated[name] = None
        prov.pop(name, None)
        notes.append(
            f"rejected_unconfirmed_next_year:{name}={value_date.isoformat()} "
            "(yearless month-day is in the recent past; ask instead of rolling)"
        )
    return updated, notes, prov


def _rebase_default_local_times(
    fields: dict[str, Any],
    *,
    provenance: dict[str, str],
    origin_tz: ZoneInfo,
    dest_tz: ZoneInfo,
    policy_tz_name: str,
    origin_tz_name: str,
    dest_tz_name: str,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """把假定本地墙钟时间重锚到出发/目的地时区。

    仅改写：
    - policy_default 字段，或
    - 仍盖政策时区（或等价固定偏移如 +08:00）的同日 08:00–18:00 窗，
      且出发城已解析到不同已知时区（模型误用家乡时区）。
    """
    updated = dict(fields)
    assumptions: list[str] = []
    prov = dict(provenance)
    policy_tz = ZoneInfo(policy_tz_name)

    dep = updated.get("departure_after")
    arr = updated.get("arrive_by")
    default_window = _is_default_day_window(dep, arr, policy_tz)
    origin_matches_policy = origin_tz.key == policy_tz.key

    field_targets: list[tuple[str, ZoneInfo, str]] = [
        ("departure_after", origin_tz, origin_tz_name),
        ("arrive_by", dest_tz, dest_tz_name),
        ("return_after", dest_tz, dest_tz_name),
        ("return_before", dest_tz, dest_tz_name),
    ]
    for name, target_tz, target_name in field_targets:
        value = updated.get(name)
        if not isinstance(value, datetime):
            continue
        if value.tzinfo is None:
            continue
        if _same_zone_or_offset(value, target_tz):
            continue
        should_rebase = prov.get(name) == "policy_default"
        if not should_rebase and default_window and name in {"departure_after", "arrive_by"}:
            should_rebase = (
                _same_zone_or_offset(value, policy_tz) and not origin_matches_policy
            )
        if not should_rebase:
            continue
        rebased = _reinterpret_local(value, target_tz)
        if rebased == value:
            continue
        updated[name] = rebased
        assumptions.append(
            f"policy_default:rebase_{name}={rebased.isoformat()} "
            f"(local wall clock re-anchored to {target_name}; was {value.isoformat()})"
        )
        prov[name] = "policy_default"

    # 若重锚跨越到达/出发边界则保持顺序。
    dep2 = updated.get("departure_after")
    arr2 = updated.get("arrive_by")
    if (
        isinstance(dep2, datetime)
        and isinstance(arr2, datetime)
        and dep2.tzinfo is not None
        and arr2.tzinfo is not None
        and arr2 <= dep2
        and default_window
    ):
        # 回退到出发地本地同日默认窗。
        day = dep2.astimezone(origin_tz).date()
        updated["departure_after"] = datetime.combine(
            day, time(DEFAULT_DEPARTURE_HOUR, 0), tzinfo=origin_tz
        )
        updated["arrive_by"] = datetime.combine(
            day, time(DEFAULT_ARRIVE_BY_HOUR, 0), tzinfo=dest_tz
        )
        assumptions.append(
            "policy_default:rebase_day_window="
            f"{updated['departure_after'].isoformat()}→{updated['arrive_by'].isoformat()} "
            f"(origin {origin_tz_name} / destination {dest_tz_name})"
        )
        prov["departure_after"] = "policy_default"
        prov["arrive_by"] = "policy_default"

    return updated, assumptions, prov


def _is_default_day_window(
    departure_after: object,
    arrive_by: object,
    policy_tz: ZoneInfo,
) -> bool:
    """是否为政策时区下的默认同日 08:00–18:00 窗。"""
    if not isinstance(departure_after, datetime) or not isinstance(arrive_by, datetime):
        return False
    if departure_after.tzinfo is None or arrive_by.tzinfo is None:
        return False
    if not _same_zone_or_offset(departure_after, policy_tz):
        return False
    if not _same_zone_or_offset(arrive_by, policy_tz):
        return False
    dep_local = departure_after
    arr_local = arrive_by
    if (
        dep_local.hour != DEFAULT_DEPARTURE_HOUR
        or dep_local.minute != 0
        or dep_local.second != 0
        or dep_local.microsecond != 0
    ):
        return False
    if (
        arr_local.hour != DEFAULT_ARRIVE_BY_HOUR
        or arr_local.minute != 0
        or arr_local.second != 0
        or arr_local.microsecond != 0
    ):
        return False
    return dep_local.date() == arr_local.date()


def _reinterpret_local(value: datetime, target_tz: ZoneInfo) -> datetime:
    """保留年月日时分秒，只换 tzinfo（不平移瞬间）。"""
    return datetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        tzinfo=target_tz,
    )


def _same_zone_or_offset(value: datetime, target_tz: ZoneInfo) -> bool:
    """若已用目标时区，或与该时区匹配的固定偏移，则为 True。"""
    if value.tzinfo is None:
        return False
    if isinstance(value.tzinfo, ZoneInfo) and value.tzinfo.key == target_tz.key:
        return True
    # 在墙钟瞬间比较偏移（处理 +08:00 vs Asia/Shanghai）。
    as_target = _reinterpret_local(value, target_tz)
    return value.utcoffset() == as_target.utcoffset()


def merge_model_fields_anti_fabrication(
    current: dict[str, Any],
    extracted: dict[str, Any],
    provided_fields: set[str],
    *,
    user_message: str | None = None,
    repair_only_missing: frozenset[str] | None = None,
    provenance: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """按防编造约束合并模型输出。

    - 普通抽取：仅应用 provided_fields 列出的字段。
    - 修复抽取：仅应用 repair_only_missing ∩ provided_fields。
    - 模型不能仅凭行程跨度制造酒店需求；须有本轮文本依据或保留先前字段。
      改写已填槽的尝试会被拒绝并记录。
    """
    merged = dict(current)
    rejected: list[str] = []
    prov = dict(provenance or {})
    allowed = set(provided_fields)
    if repair_only_missing is not None:
        allowed &= set(repair_only_missing)
    hotel_grounded = user_message is None or message_requests_hotel(user_message)
    return_grounded = user_message is None or _message_suggests_return(user_message)
    explicitly_one_way = bool(
        user_message is not None and _message_explicitly_one_way(user_message)
    )
    if explicitly_one_way:
        for name in ("return_after", "return_before"):
            merged[name] = None
            prov.pop(name, None)

    for name in _SLOT_FIELDS:
        if name not in provided_fields:
            continue
        if name not in allowed:
            rejected.append(name)
            continue
        value = extracted.get(name)
        if name in {"return_after", "return_before"} and explicitly_one_way:
            if value is not None:
                rejected.append(f"{name}:explicit_one_way")
            continue
        if (
            name in {"return_after", "return_before"}
            and current.get(name) in (None, "", [])
            and not return_grounded
        ):
            if value is not None:
                rejected.append(f"{name}:ungrounded_return")
            continue
        if (
            name in {"hotel_check_in", "hotel_check_out"}
            and current.get(name) in (None, "", [])
            and not hotel_grounded
        ):
            rejected.append(f"{name}:ungrounded_hotel")
            continue
        if (
            name == "hard_constraints"
            and not hotel_grounded
            and "hotel_required" not in (current.get("hard_constraints") or ())
            and value
        ):
            constraints = list(value)
            value = [item for item in constraints if item != "hotel_required"]
            if len(value) != len(constraints):
                rejected.append("hotel_required:ungrounded_hotel")
        if value in (None, "", []):
            # 修复中的显式 null 不得抹掉已填槽。
            if repair_only_missing is not None and merged.get(name) not in (None, "", []):
                rejected.append(f"{name}:null_rejected")
                continue
            if repair_only_missing is not None:
                # 保持缺失；模型承认无法落地该值。
                continue
        if repair_only_missing is not None and name not in repair_only_missing:
            rejected.append(name)
            continue
        # 拒绝用修复覆盖非空旧值，除非该槽本就在缺失列表。
        if (
            repair_only_missing is not None
            and current.get(name) not in (None, "", [])
            and name not in repair_only_missing
        ):
            rejected.append(name)
            continue
        merged[name] = value
        if repair_only_missing is not None:
            prov[name] = "model_repair"
        else:
            prov.setdefault(name, "model_extract")
    return merged, rejected, prov


def build_repair_context(
    *,
    missing: tuple[str, ...],
    fields: dict[str, Any],
    user_message: str,
    classification: str,
    tool_use_error: str | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """修复抽取调用的上下文块（禁止自由编造）。

    设置 ``tool_use_error``（Claude 参数环）时，向模型展示结构化校验失败，
    而非模糊的「缺了点什么」。
    """
    payload: dict[str, Any] = {
        "mode": "repair",
        "missing_fields": list(missing),
        "current_fields": _json_safe(fields),
        "classification": classification,
        "user_message": user_message,
        "rules": [
            "ONLY fill fields listed in missing_fields.",
            "Every filled value MUST be grounded in the user_message or current_fields.",
            (
                "If the user message does not support a missing field, leave it null "
                "and do not list it in provided_fields."
            ),
            (
                "Do NOT invent cities, dates, seat classes, prices, policy outcomes, "
                "or employee levels."
            ),
            "Do NOT change fields that are already filled unless they are in missing_fields.",
            "Identity/level/approval chatter must not clear trip slots.",
        ],
    }
    if tool_use_error:
        payload["is_error"] = True
        payload["tool_use_error"] = tool_use_error
        payload["pattern"] = "claude_code_param_loop_v1"
    if issues:
        payload["issues"] = list(issues)
    return payload


def repair_system_instructions(repair: dict[str, Any]) -> str:
    """修复用系统提示；有 Claude 风格 tool_use_error 时优先使用。"""
    if repair.get("pattern") == "claude_code_param_loop_v1" or repair.get("tool_use_error"):
        from corporate_travel_agent.agent.param_extraction_loop import (
            repair_system_instructions_from_loop,
        )

        return repair_system_instructions_from_loop(repair)

    missing = ", ".join(repair.get("missing_fields") or [])
    return (
        "REPAIR MODE: You previously extracted corporate travel intent but business validation "
        f"found missing search-critical fields: [{missing}]. "
        "Re-read the user message and current_fields. "
        "Fill ONLY those missing fields when the user text (or already extracted dates) "
        "clearly supports them. "
        "If unsupported, leave null — never invent. "
        "Do not invent inventory, prices, approvals, or policy outcomes. "
        "provided_fields must list only fields you are newly grounding in this repair. "
        "Repair context JSON: "
        + __import__("json").dumps(repair, ensure_ascii=False, default=str, sort_keys=True)
    )


def _return_anchor_date(
    fields: dict[str, Any],
    reference_time: datetime,
    local_tz: ZoneInfo | None = None,
) -> date | None:
    """从已有字段推导返程锚日；无法则 None（不编造日历日）。"""
    for key in ("return_after", "return_before", "arrive_by", "departure_after"):
        value = fields.get(key)
        if isinstance(value, datetime):
            if local_tz is not None and value.tzinfo is not None:
                return value.astimezone(local_tz).date()
            return value.date()
        if isinstance(value, date):
            return value
    # 多日但无日期：不能编造日历日
    _ = reference_time
    return None


def _message_requested_hotel_nights(message: str) -> int | None:
    """解析「住一晚/两晚」；无则 None。"""
    text = message or ""
    if _TWO_NIGHTS_RE.search(text):
        return 2
    if _ONE_NIGHT_RE.search(text):
        return 1
    return None


def _message_requests_one_night(message: str) -> bool:
    return _message_requested_hotel_nights(message) == 1


def message_is_return_scoped_relative_day(message: str) -> bool:
    """明天/后天附着于返程修订而非去程日时为 True。"""
    return bool(_RETURN_SCOPED_RELATIVE_DAY_RE.search(message or ""))


def apply_followup_slot_revisions(
    fields: dict[str, Any],
    *,
    user_message: str,
    reference_time: datetime,
    timezone_name: str,
    provenance: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """应用有依据的选后槽位修订，不编造新路线。"""
    updated = dict(fields)
    notes: list[str] = []
    prov = dict(provenance or {})
    origin_tz_name, dest_tz_name = route_timezones(
        updated.get("origin") if isinstance(updated.get("origin"), str) else None,
        updated.get("destination") if isinstance(updated.get("destination"), str) else None,
        fallback=timezone_name,
    )
    dest_tz = ZoneInfo(dest_tz_name)
    origin_tz = ZoneInfo(origin_tz_name)

    nights = _message_requested_hotel_nights(user_message)
    if nights:
        hard = list(updated.get("hard_constraints") or [])
        if "hotel_required" not in hard:
            hard.append("hotel_required")
        updated["hard_constraints"] = hard
        updated["lodging_requirement"] = LodgingRequirement.REQUIRED.value
        check_in = updated.get("hotel_check_in")
        if not isinstance(check_in, date) or isinstance(check_in, datetime):
            arrival = updated.get("arrive_by") or updated.get("departure_after")
            if isinstance(arrival, datetime):
                local = arrival.astimezone(dest_tz) if arrival.tzinfo else arrival.replace(
                    tzinfo=dest_tz
                )
                check_in = local.date()
                updated["hotel_check_in"] = check_in
                prov["hotel_check_in"] = "followup_revision"
        if isinstance(check_in, date) and not isinstance(check_in, datetime):
            check_out = check_in + timedelta(days=nights)
            updated["hotel_check_out"] = check_out
            notes.append(
                f"followup_revision:hotel_nights={nights} "
                f"{check_in.isoformat()}→{check_out.isoformat()}"
            )
            prov["hotel_check_out"] = "followup_revision"
            prov["lodging_requirement"] = "followup_revision"

    meeting_clock = _parse_meeting_revision_clock(user_message)
    if meeting_clock is not None:
        arrive = updated.get("arrive_by")
        anchor = arrive if isinstance(arrive, datetime) else updated.get("departure_after")
        if isinstance(anchor, datetime):
            local = anchor.astimezone(dest_tz) if anchor.tzinfo else anchor.replace(
                tzinfo=dest_tz
            )
            updated["arrive_by"] = datetime.combine(local.date(), meeting_clock, tzinfo=dest_tz)
            notes.append(
                f"followup_revision:meeting_time={updated['arrive_by'].isoformat()} "
                f"({dest_tz_name})"
            )
            prov["arrive_by"] = "followup_revision"
            depart = updated.get("departure_after")
            new_arrive = updated["arrive_by"]
            if (
                isinstance(depart, datetime)
                and depart.tzinfo is not None
                and new_arrive <= depart
            ):
                same_day = datetime.combine(
                    new_arrive.astimezone(origin_tz).date(),
                    time(DEFAULT_DEPARTURE_HOUR, 0),
                    tzinfo=origin_tz,
                )
                if same_day < new_arrive:
                    updated["departure_after"] = same_day
                    notes.append(
                        f"followup_revision:departure_after={same_day.isoformat()} "
                        "(kept before new meeting time)"
                    )
                    prov["departure_after"] = "followup_revision"

    if (
        _message_is_return_revision(user_message)
        and not _message_explicitly_one_way(user_message)
    ):
        return_window = _resolve_return_revision_window(
            updated,
            user_message=user_message,
            reference_time=reference_time,
            dest_tz=dest_tz,
        )
        if return_window is not None:
            ret_after, ret_before = return_window
            updated["return_after"] = ret_after
            updated["return_before"] = ret_before
            notes.append(
                f"followup_revision:return_revision="
                f"{ret_after.isoformat()}→{ret_before.isoformat()} ({dest_tz_name})"
            )
            prov["return_after"] = "followup_revision"
            prov["return_before"] = "followup_revision"

    return updated, notes, prov


def _parse_meeting_revision_clock(message: str) -> time | None:
    if not _MEETING_REVISION_RE.search(message or ""):
        return None
    match = _CLOCK_FRAGMENT_RE.search(message or "")
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    period = (match.group("period") or "").strip()
    if period in {"下午", "傍晚", "晚上"} and 1 <= hour <= 11:
        hour += 12
    elif period == "中午" and hour in {0, 12}:
        hour = 12
    if hour > 23 or hour < 0 or minute > 59:
        return None
    return time(hour, minute)


def _message_is_return_revision(message: str) -> bool:
    return bool(_RETURN_REVISION_RE.search(message or ""))


def _relative_return_offset_days(message: str) -> int | None:
    text = message or ""
    if "后天" in text or re.search(r"day after tomorrow", text, re.I):
        return 2
    if "明天" in text or re.search(r"\btomorrow\b", text, re.I):
        return 1
    return None


def _resolve_return_revision_window(
    fields: dict[str, Any],
    *,
    user_message: str,
    reference_time: datetime,
    dest_tz: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    offset = _relative_return_offset_days(user_message)
    if offset is None:
        return None
    ref = reference_time
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dest_tz)
    else:
        ref = ref.astimezone(dest_tz)
    clock_day = ref.date() + timedelta(days=offset)
    arrive = fields.get("arrive_by") or fields.get("departure_after")
    if isinstance(arrive, datetime):
        arrive_day = arrive.astimezone(dest_tz).date() if arrive.tzinfo else arrive.date()
        day = clock_day if clock_day >= arrive_day else arrive_day + timedelta(days=offset)
    else:
        day = clock_day
    if re.search(r"晚上|今晚|tonight", user_message or "", re.I):
        after_hour, before_hour = 18, 23
    elif re.search(r"下午|afternoon", user_message or "", re.I):
        after_hour, before_hour = DEFAULT_RETURN_AFTER_HOUR, DEFAULT_RETURN_BEFORE_HOUR
    else:
        after_hour, before_hour = 18, 23
    return (
        datetime.combine(day, time(after_hour, 0), tzinfo=dest_tz),
        datetime.combine(day, time(before_hour, 0), tzinfo=dest_tz),
    )


def _message_suggests_multi_day(message: str) -> bool:
    text = message.lower()
    markers = (
        "往返",
        "返回",
        "返程",
        "round trip",
        "return on",
        "return",
        " overnight",
        "multi-day",
        "多日",
        " overnight",
        "check out",
        "退房",
    )
    # "Aug 20-22" / "20日至22日"
    import re

    if re.search(r"\b\d{1,2}\s*[-–—至到]\s*\d{1,2}\b", message):
        return True
    if re.search(r"\b\d{1,2}/\d{1,2}\s*[-–—]\s*\d{1,2}/\d{1,2}\b", message):
        return True
    return any(m in text for m in markers if m != "return") or (
        "return" in text and ("aug" in text or "月" in message or "日" in message)
    )


def _message_suggests_return(message: str) -> bool:
    """用户是否表达返程/往返意图（含否定检测）。"""
    text = message.casefold()
    english_return_time = re.search(
        r"\breturn\s+(?:(?:next|this)\s+)?(?:today|tomorrow|tonight|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
        r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
        r"\d{1,4}[-/])",
        text,
    )
    english_return_request = re.search(
        r"\b(?:also\s+)?(?:need|want|book|find|search(?:\s+for)?|add|include)\s+"
        r"(?:me\s+)?(?:a\s+)?return\s+(?:flight|trip|leg|journey)\b",
        text,
    )
    english_round_trip = _has_unnegated_phrase(
        text,
        r"\bround[- ]trip\b",
    )
    english_back = _has_unnegated_phrase(
        text,
        r"\b(?:come|fly|travel|head)\s+back\b",
    )
    chinese_return = _has_unnegated_chinese_return(message)
    return bool(
        chinese_return
        or english_round_trip
        or english_return_request
        or english_return_time
        or re.search(r"\breturn\s+(?:on|by|after|before|to|from)\b", text)
        or english_back
        or re.search(r"\bback\s+(?:on|by|after|before|to|from)\b", text)
        or re.search(
            r"(?:当天|次日|随后|然后|上午|下午|晚上|周[一二三四五六日天]|"
            r"\d{1,2}\s*[日号]).{0,8}(?:返回|返程|回来|回程)",
            message,
        )
        or re.search(r"(?:当天)?(?:下午|晚上)回", message)
        or re.search(r"当天回", message)
        or re.search(r"\d{1,2}[日号]回", message)
        or re.search(
            r"(?:我要|我想|我需要|还要|还得|需要|计划|将会|会)返回(?:到|去)?"
            r"(?!结果|选项|数据|内容|页面|列表|行程|酒店|租车|车辆)"
            r"[\u4e00-\u9fff]{2,}",
            message,
        )
    )


def _has_unnegated_phrase(text: str, pattern: str) -> bool:
    for match in re.finditer(pattern, text):
        prefix = text[max(0, match.start() - 32) : match.start()]
        if re.search(
            r"(?:\bno|\bnot|\bwithout|\bdo\s+not|\bdon['’]?t)\s+"
            r"(?:(?:want|need|book)\s+)?(?:a\s+)?$",
            prefix,
        ):
            continue
        return True
    return False


def _has_unnegated_chinese_return(message: str) -> bool:
    for match in re.finditer(r"往返|返程|回程|回来", message):
        prefix = message[max(0, match.start() - 8) : match.start()]
        if re.search(r"(?:不要|无需|不需要|不用|不想|别)\s*$", prefix):
            continue
        return True
    return False


def _message_direction_evidence(message: str) -> str:
    """返回 ROUND_TRIP / ONE_WAY / AMBIGUOUS。"""
    text = message.casefold()
    one_way_negated = bool(
        re.search(r"\bnot\s+(?:a\s+)?one[- ]?way\b", text)
        or re.search(r"\b(?:isn['’]?t|isnt)\s+(?:a\s+)?one[- ]?way\b", text)
        or re.search(
            r"\b(?:do\s+not|don['’]?t)\s+(?:want|need|book)\s+"
            r"(?:a\s+)?one[- ]?way\b",
            text,
        )
        or re.search(r"\bnot\s+(?:the\s+)?outbound(?:\s+leg)?\s+only\b", text)
        or re.search(r"\bnot\s+(?:just|only)\s+(?:the\s+)?outbound\b", text)
        or re.search(
            r"\bno\s+return\s+(?:flight|trip|leg)\s+"
            r"(?:was|is|has\s+been)\s+(?:shown|found|available|offered)\b",
            text,
        )
        or re.search(
            r"(?:不是|并非|别|不要|不想(?:订|预订|买)?)(?:只去|单程)",
            message,
        )
        or any(
            token in message
            for token in ("不是单程", "并非单程", "不只去", "不只是去", "不仅去")
        )
    )
    one_way_requested = not one_way_negated and bool(
        re.search(r"\bone[- ]?way\b", text)
        or re.search(r"\bno\s+return(?:\s+(?:flight|trip|leg))?\b", text)
        or re.search(r"\boutbound(?:\s+leg)?\s+only\b", text)
        or any(
            token in message
            for token in (
                "单程",
                "只去",
                "只订去程",
                "不要返程",
                "不需要返程",
                "无需返程",
                "不返回",
            )
        )
    )
    return_requested = _message_suggests_return(message)
    if one_way_requested and return_requested:
        return "AMBIGUOUS"
    if return_requested:
        return "ROUND_TRIP"
    if one_way_requested:
        return "ONE_WAY"
    return "AMBIGUOUS"


def _message_explicitly_one_way(message: str) -> bool:
    """是否明确要求单程。"""
    return _message_direction_evidence(message) == "ONE_WAY"


def _message_has_trip_day_signal(message: str) -> bool:
    import re

    if re.search(r"\d{1,2}\s*日", message):
        return True
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", message, re.I):
        return True
    if re.search(r"\d{4}-\d{2}-\d{2}", message):
        return True
    return "明天" in message or "下周" in message or "tomorrow" in message.lower()


def _message_has_meeting_signal(message: str) -> bool:
    text = message.lower()
    return any(token in text for token in ("开会", "会议", "meeting", "am ", "pm ", "点"))


def _json_safe(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, (datetime, date)):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out
