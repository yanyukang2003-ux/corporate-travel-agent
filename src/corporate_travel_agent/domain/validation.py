"""domain.validation：行程请求字段完整性与冲突校验（不依赖框架）。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .constraints import (
    SUPPORTED_HARD_CONSTRAINTS,
    SUPPORTED_SOFT_PREFERENCES,
    WHOLE_JOURNEY_ONLY_REQUIREMENTS,
)
from .enums import BookingScope
from .models import TripRequestVersion


class TripRequestValidationError(ValueError):
    """行程请求校验失败：携带缺失字段与冲突说明。"""

    def __init__(self, missing: tuple[str, ...], conflicts: tuple[str, ...]) -> None:
        self.missing = missing
        self.conflicts = conflicts
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if conflicts:
            details.append("conflicts: " + "; ".join(conflicts))
        super().__init__("Invalid trip request (" + " | ".join(details) + ")")


@dataclass(frozen=True, slots=True)
class TripRequestValidation:
    """一次校验结果：missing / conflicts；valid 为二者皆空。"""

    missing: tuple[str, ...]
    conflicts: tuple[str, ...]

    @property
    def valid(self) -> bool:
        """是否可进入搜索/规划。"""
        return not self.missing and not self.conflicts

    def require_valid(self) -> None:
        """无效时抛出 TripRequestValidationError。"""
        if not self.valid:
            raise TripRequestValidationError(self.missing, self.conflicts)


def validate_trip_request(request: TripRequestVersion) -> TripRequestValidation:
    """校验已构造的 TripRequestVersion（含 version≥1）。"""
    if request.version < 1:
        return TripRequestValidation((), ("request version must be at least 1",))
    return validate_trip_request_values(
        {
            "origin": request.origin,
            "destination": request.destination,
            "journey": request.journey,
            "stays": request.stays,
            "departure_after": request.departure_after,
            "arrive_by": request.arrive_by,
            "return_after": request.return_after,
            "return_before": request.return_before,
            "booking_scope": request.booking_scope,
            "hotel_check_in": request.hotel_check_in,
            "hotel_check_out": request.hotel_check_out,
            "hard_constraints": request.hard_constraints,
            "soft_preferences": request.soft_preferences,
            "scoped_hard_constraints": request.scoped_hard_constraints,
            "scoped_soft_preferences": request.scoped_soft_preferences,
        }
    )


def validate_trip_request_values(fields: Mapping[str, Any]) -> TripRequestValidation:
    """对字段映射做类型、时区、成对性与约束兼容性检查；不填充缺失值。"""
    required = ("origin", "destination", "departure_after", "arrive_by")
    missing = [name for name in required if fields.get(name) in (None, "")]
    conflicts: list[str] = []

    origin = fields.get("origin")
    destination = fields.get("destination")
    if origin is not None and not isinstance(origin, str):
        conflicts.append("origin must be a string")
    if destination is not None and not isinstance(destination, str):
        conflicts.append("destination must be a string")
    if isinstance(origin, str) and not origin.strip():
        missing.append("origin")
    if isinstance(destination, str) and not destination.strip():
        missing.append("destination")
    if (
        origin
        and destination
        and str(origin).strip().casefold() == str(destination).strip().casefold()
    ):
        conflicts.append("origin and destination must be different")

    departure_after = fields.get("departure_after")
    arrive_by = fields.get("arrive_by")
    if departure_after is not None and not isinstance(departure_after, datetime):
        conflicts.append("departure_after must be a datetime")
    if arrive_by is not None and not isinstance(arrive_by, datetime):
        conflicts.append("arrive_by must be a datetime")
    _require_aware(departure_after, "departure_after", conflicts)
    _require_aware(arrive_by, "arrive_by", conflicts)
    if (
        isinstance(departure_after, datetime)
        and isinstance(arrive_by, datetime)
        and _timezone_aware(departure_after)
        and _timezone_aware(arrive_by)
        and arrive_by <= departure_after
    ):
        conflicts.append("arrive_by must be later than departure_after")

    return_after = fields.get("return_after")
    return_before = fields.get("return_before")
    raw_scope = fields.get("booking_scope")
    scope: BookingScope | None = None
    if isinstance(raw_scope, BookingScope):
        scope = raw_scope
    elif isinstance(raw_scope, str):
        try:
            scope = BookingScope(raw_scope)
        except ValueError:
            conflicts.append("booking_scope is invalid")
    elif raw_scope is not None:
        conflicts.append("booking_scope is invalid")
    if scope is None and raw_scope is None:
        scope = (
            BookingScope.ROUND_TRIP
            if return_after is not None or return_before is not None
            else BookingScope.OUTBOUND_ONLY
        )
    if return_after is not None and not isinstance(return_after, datetime):
        conflicts.append("return_after must be a datetime")
    if return_before is not None and not isinstance(return_before, datetime):
        conflicts.append("return_before must be a datetime")
    if (return_after is None) != (return_before is None):
        missing.append("return_before" if return_before is None else "return_after")
    if scope is BookingScope.ROUND_TRIP:
        if return_after is None:
            missing.append("return_after")
        if return_before is None:
            missing.append("return_before")
    elif scope in {BookingScope.OUTBOUND_ONLY, BookingScope.RETURN_ONLY} and (
        return_after is not None or return_before is not None
    ):
        conflicts.append("single-leg booking must not contain return fields")
    _require_aware(return_after, "return_after", conflicts)
    _require_aware(return_before, "return_before", conflicts)
    if (
        isinstance(return_after, datetime)
        and isinstance(return_before, datetime)
        and _timezone_aware(return_after)
        and _timezone_aware(return_before)
        and return_before <= return_after
    ):
        conflicts.append("return_before must be later than return_after")

    hotel_check_in = fields.get("hotel_check_in")
    hotel_check_out = fields.get("hotel_check_out")
    if hotel_check_in is not None and (
        not isinstance(hotel_check_in, date) or isinstance(hotel_check_in, datetime)
    ):
        conflicts.append("hotel_check_in must be a date")
    if hotel_check_out is not None and (
        not isinstance(hotel_check_out, date) or isinstance(hotel_check_out, datetime)
    ):
        conflicts.append("hotel_check_out must be a date")
    if (hotel_check_in is None) != (hotel_check_out is None):
        missing.append("hotel_check_out" if hotel_check_out is None else "hotel_check_in")
    if (
        isinstance(hotel_check_in, date)
        and isinstance(hotel_check_out, date)
        and hotel_check_out <= hotel_check_in
    ):
        conflicts.append("hotel_check_out must be later than hotel_check_in")

    hard_constraints = _as_tuple(fields.get("hard_constraints"))
    soft_preferences = _as_tuple(fields.get("soft_preferences"))
    invalid_hard_types = [item for item in hard_constraints if not isinstance(item, str)]
    invalid_soft_types = [item for item in soft_preferences if not isinstance(item, str)]
    if invalid_hard_types:
        conflicts.append("hard constraint names must be strings")
    if invalid_soft_types:
        conflicts.append("soft preference names must be strings")
    normalized_hard = {item for item in hard_constraints if isinstance(item, str)}
    normalized_soft = {item for item in soft_preferences if isinstance(item, str)}
    unknown_hard = sorted(normalized_hard - SUPPORTED_HARD_CONSTRAINTS)
    unknown_soft = sorted(normalized_soft - SUPPORTED_SOFT_PREFERENCES)
    if unknown_hard:
        conflicts.append("unsupported hard constraints: " + ", ".join(unknown_hard))
    if unknown_soft:
        conflicts.append("unsupported soft preferences: " + ", ".join(unknown_soft))
    if {"train_only", "flight_only"}.issubset(normalized_hard):
        conflicts.append("train_only and flight_only cannot both be required")
    if "hotel_required" in normalized_hard:
        if hotel_check_in is None:
            missing.append("hotel_check_in")
        if hotel_check_out is None:
            missing.append("hotel_check_out")

    conflicts.extend(_journey_conflicts(fields.get("journey")))
    conflicts.extend(
        _stay_conflicts(
            fields.get("stays"),
            journey=fields.get("journey"),
            hotel_check_in=hotel_check_in,
            hotel_check_out=hotel_check_out,
        )
    )
    leg_count = _expected_leg_count(fields, scope)
    conflicts.extend(
        _scope_conflicts(
            fields.get("scoped_hard_constraints"),
            flat_names=normalized_hard,
            supported=SUPPORTED_HARD_CONSTRAINTS,
            leg_count=leg_count,
            label="hard constraint",
        )
    )
    conflicts.extend(
        _scope_conflicts(
            fields.get("scoped_soft_preferences"),
            flat_names=normalized_soft,
            supported=SUPPORTED_SOFT_PREFERENCES,
            leg_count=leg_count,
            label="soft preference",
        )
    )

    return TripRequestValidation(
        tuple(dict.fromkeys(missing)),
        tuple(dict.fromkeys(conflicts)),
    )


#: 一次申请最多能安排的航段数。抄 Amadeus 的上限，把“做不了”从模糊的能力边界
#: 变成一个可以确定性判定的数字，也才能给用户一句可执行的答复。
MAX_JOURNEY_LEGS = 6


def _journey_conflicts(journey: Any) -> list[str]:
    """校验显式给出的航段序列：段数、串接、时序。

    这是多段行程唯一的确定性守门人。顺序检查只拦整段颠倒的窗口（下一段的到达时限早于
    上一段最早出发）；班次之间接不接得上由可行性校验按实际报价判。
    """
    if not journey:
        return []
    if not isinstance(journey, (list, tuple)):
        return ["journey must be a sequence of legs"]
    legs = list(journey)
    problems: list[str] = []
    if len(legs) > MAX_JOURNEY_LEGS:
        problems.append(
            f"journey has {len(legs)} legs; at most {MAX_JOURNEY_LEGS} can be arranged "
            "in one request"
        )
    for index, leg in enumerate(legs):
        origin = getattr(leg, "origin", None)
        destination = getattr(leg, "destination", None)
        depart_after = getattr(leg, "depart_after", None)
        arrive_before = getattr(leg, "arrive_before", None)
        if not isinstance(origin, str) or not origin.strip():
            problems.append(f"journey leg {index} is missing an origin")
        if not isinstance(destination, str) or not destination.strip():
            problems.append(f"journey leg {index} is missing a destination")
        if (
            isinstance(origin, str)
            and isinstance(destination, str)
            and origin.strip().casefold() == destination.strip().casefold()
        ):
            problems.append(f"journey leg {index} starts and ends in the same city")
        if not isinstance(depart_after, datetime) or not isinstance(arrive_before, datetime):
            problems.append(f"journey leg {index} needs a departure and arrival window")
            continue
        if not _timezone_aware(depart_after) or not _timezone_aware(arrive_before):
            problems.append(f"journey leg {index} windows must be timezone-aware")
            continue
        if arrive_before <= depart_after:
            problems.append(f"journey leg {index} must arrive after it departs")
        if index == 0:
            continue
        previous = legs[index - 1]
        previous_departure = getattr(previous, "depart_after", None)
        # 窗口是搜索窗口，不是实际班次：去程"最晚 7 号 10 点到"和返程"最早 6 号 17 点走"可以
        # 同时成立（6 号早上到、6 号晚上回）。真正说不通的是**整段颠倒**——下一段必须到达的
        # 时限，早于上一段最早出发的时刻；班次之间接不接得上由可行性校验按实际报价判。
        if isinstance(previous_departure, datetime) and _timezone_aware(previous_departure):
            if arrive_before <= previous_departure:
                problems.append(
                    f"journey leg {index} must arrive before leg {index - 1} can even depart"
                )
    return problems



def _stay_conflicts(
    stays: Any,
    *,
    journey: Any,
    hotel_check_in: Any,
    hotel_check_out: Any,
) -> list[str]:
    """校验显式给出的住宿站序列：城市、日期、站数，以及与扁平字段是否讲同一句话。

    住宿站是继航段之后第二个"结构化字段盖过扁平字段"的地方，所以守门的规矩
    和 `_journey_conflicts` 一样：**没人替它兜底，它自己必须站得住。**
    """
    if not stays:
        return []
    if not isinstance(stays, (list, tuple)):
        return ["stays must be a sequence of lodging stops"]
    items = list(stays)
    problems: list[str] = []

    # 一趟行程最多在 N-1 个中途点过夜（最后一段落地就到家了）。没给 journey 时
    # 按往返的一站算——这正是扁平字段能表达的极限。
    leg_count = len(journey) if isinstance(journey, (list, tuple)) and journey else 2
    if len(items) > max(leg_count - 1, 1):
        problems.append(
            f"stays has {len(items)} stops; a {leg_count}-leg journey can stay over at "
            f"most {max(leg_count - 1, 1)} times"
        )

    for index, stay in enumerate(items):
        city = getattr(stay, "city", None)
        check_in = getattr(stay, "check_in", None)
        check_out = getattr(stay, "check_out", None)
        if not isinstance(city, str) or not city.strip():
            problems.append(f"stay {index} is missing a city")
        if (
            not isinstance(check_in, date)
            or isinstance(check_in, datetime)
            or not isinstance(check_out, date)
            or isinstance(check_out, datetime)
        ):
            problems.append(f"stay {index} needs a check-in and check-out date")
            continue
        if check_out <= check_in:
            problems.append(f"stay {index} must check out after it checks in")
        if index == 0:
            continue
        previous_out = getattr(items[index - 1], "check_out", None)
        if isinstance(previous_out, date) and check_in < previous_out:
            problems.append(f"stay {index} checks in before stay {index - 1} checks out")

    # 两个视图必须讲同一句话：第一站就是扁平字段说的那一次住宿。
    first = items[0]
    first_in = getattr(first, "check_in", None)
    first_out = getattr(first, "check_out", None)
    if hotel_check_in is not None and first_in != hotel_check_in:
        problems.append("stays[0] check-in disagrees with hotel_check_in")
    if hotel_check_out is not None and first_out != hotel_check_out:
        problems.append("stays[0] check-out disagrees with hotel_check_out")
    if hotel_check_in is None and hotel_check_out is None:
        problems.append("stays were given without hotel_check_in/hotel_check_out")
    return problems


def _expected_leg_count(fields: Mapping[str, Any], scope: BookingScope | None) -> int:
    """这次请求会执行几段：显式给了航段就数它，否则按预订范围推。"""
    journey = fields.get("journey")
    if isinstance(journey, (list, tuple)) and journey:
        return len(journey)
    return 2 if scope is BookingScope.ROUND_TRIP else 1


def _scope_conflicts(
    scoped: Any,
    *,
    flat_names: set[str],
    supported: frozenset[str],
    leg_count: int,
    label: str,
) -> list[str]:
    """校验带作用域的要求：名字在词表内、航段号存在、两种视图说的是同一批名字。

    最后一条是关键：扁平列表仍然是政策引擎、评测集和旧持久化载荷读到的东西。
    两个视图一旦讲不同的话，读哪一个就成了运气问题，所以宁可判冲突也不放行。
    """
    if not scoped:
        return []
    if not isinstance(scoped, (list, tuple)):
        return [f"scoped {label}s must be a sequence"]
    problems: list[str] = []
    names: set[str] = set()
    for item in scoped:
        name = getattr(item, "name", None)
        leg_index = getattr(item, "leg_index", None)
        if not isinstance(name, str) or not name.strip():
            problems.append(f"scoped {label} is missing a name")
            continue
        names.add(name)
        if name not in supported:
            problems.append(f"unsupported {label}s: {name}")
            continue
        if leg_index is None:
            continue
        if not isinstance(leg_index, int) or isinstance(leg_index, bool):
            problems.append(f"{label} {name} has a non-numeric leg index")
            continue
        if name in WHOLE_JOURNEY_ONLY_REQUIREMENTS:
            problems.append(f"{label} {name} applies to the whole journey, not one leg")
            continue
        if leg_index < 0 or leg_index >= leg_count:
            problems.append(
                f"{label} {name} names leg {leg_index}, but this journey has "
                f"{leg_count} leg(s)"
            )
    if names and names != flat_names:
        problems.append(
            f"scoped {label}s and the flat {label} list must name the same requirements"
        )
    return problems


def _require_aware(value: object, field_name: str, conflicts: list[str]) -> None:
    """若为 datetime 则必须带时区。"""
    if isinstance(value, datetime) and not _timezone_aware(value):
        conflicts.append(f"{field_name} must include a timezone")


def _timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _as_tuple(value: object) -> tuple[object, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


# ---------------------------------------------------------------------------
# 下单确认回填
# ---------------------------------------------------------------------------

ORDER_REFERENCE_MAX_LENGTH = 64
ORDER_REFERENCE_MAX_COUNT = 10
CONFIRMATION_NOTE_MAX_LENGTH = 500
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")


class BookingConfirmationValidationError(ValueError):
    """下单确认回填不合法：订单号、金额、币种或时间有问题。"""


@dataclass(frozen=True, slots=True)
class BookingConfirmationValues:
    """校验并规范化后的回填值。"""

    order_references: tuple[str, ...]
    total_amount: Decimal
    currency: str
    booked_at: datetime
    note: str | None


def validate_booking_confirmation_values(
    *,
    order_references: Sequence[str],
    total_amount: Decimal,
    currency: str,
    booked_at: datetime | None,
    now: datetime,
    note: str | None = None,
) -> BookingConfirmationValues:
    """把员工填的东西规范化，不合法就拒绝。

    **这里只管形状，不管真假**——订单号是不是真的存在、金额是不是真的付了，系统核不了。

    规矩：

    - 订单号去首尾空白、去重、去空串；至少一个，最多 10 个，每个不超过 64 字符，
      不含换行和控制字符（它会进审计、进报表，一个换行就能把一行表撑坏）；
    - 金额是有限的十进制数，不为负；**允许 0**——积分票、协议价预付都可能是 0，
      拒绝它会把真实情况挡在门外；
    - 币种三个大写字母；**不要求和方案币种一致**（员工用人民币付了美元报价是真事），
      差额算不算得出来是读的人的事，见 `TripTask.booking_cost_variance`；
    - 下单时刻必须带时区，且不晚于现在；没填就等于现在。
    """
    references: list[str] = []
    for raw in order_references:
        value = raw.strip()
        if not value or value in references:
            continue
        if len(value) > ORDER_REFERENCE_MAX_LENGTH:
            raise BookingConfirmationValidationError(
                f"order reference exceeds {ORDER_REFERENCE_MAX_LENGTH} characters"
            )
        if any(ord(ch) < 32 or ch == "\x7f" for ch in value):
            raise BookingConfirmationValidationError(
                "order reference contains control characters"
            )
        references.append(value)
    if not references:
        raise BookingConfirmationValidationError("at least one order reference is required")
    if len(references) > ORDER_REFERENCE_MAX_COUNT:
        raise BookingConfirmationValidationError(
            f"at most {ORDER_REFERENCE_MAX_COUNT} order references are accepted"
        )

    if not isinstance(total_amount, Decimal) or not total_amount.is_finite():
        raise BookingConfirmationValidationError("total amount must be a finite decimal")
    if total_amount < 0:
        raise BookingConfirmationValidationError("total amount cannot be negative")

    code = currency.strip()
    if not _CURRENCY_CODE.match(code):
        raise BookingConfirmationValidationError(
            "currency must be a three-letter upper-case code"
        )

    if not _is_timezone_aware(now):
        raise BookingConfirmationValidationError("reference time must be timezone-aware")
    if booked_at is None:
        booked_at = now
    elif not _is_timezone_aware(booked_at):
        raise BookingConfirmationValidationError("booked_at must be timezone-aware")
    elif booked_at > now:
        raise BookingConfirmationValidationError("booked_at cannot be in the future")

    cleaned_note = (note or "").strip() or None
    if cleaned_note is not None and len(cleaned_note) > CONFIRMATION_NOTE_MAX_LENGTH:
        raise BookingConfirmationValidationError(
            f"note exceeds {CONFIRMATION_NOTE_MAX_LENGTH} characters"
        )

    return BookingConfirmationValues(
        order_references=tuple(references),
        total_amount=total_amount,
        currency=code,
        booked_at=booked_at,
        note=cleaned_note,
    )


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None
