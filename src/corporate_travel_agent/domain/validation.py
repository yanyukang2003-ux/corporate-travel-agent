"""domain.validation：行程请求字段完整性与冲突校验（不依赖框架）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .constraints import SUPPORTED_HARD_CONSTRAINTS, SUPPORTED_SOFT_PREFERENCES
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
            "departure_after": request.departure_after,
            "arrive_by": request.arrive_by,
            "return_after": request.return_after,
            "return_before": request.return_before,
            "booking_scope": request.booking_scope,
            "hotel_check_in": request.hotel_check_in,
            "hotel_check_out": request.hotel_check_out,
            "hard_constraints": request.hard_constraints,
            "soft_preferences": request.soft_preferences,
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

    return TripRequestValidation(
        tuple(dict.fromkeys(missing)),
        tuple(dict.fromkeys(conflicts)),
    )


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
