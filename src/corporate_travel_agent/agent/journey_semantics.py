"""把自然语言中的行程范围归一为真实方向航段语义。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from corporate_travel_agent.domain.enums import BookingScope

_RETURN_FROM_CITY_RE = re.compile(
    r"从(?P<city>[^，,。；;！？!?\n]{1,20}?)(?:回来|返回|返程|回(?=[\u4e00-\u9fffA-Za-z]))"
)
_RETURN_FROM_CITY_EN_RE = re.compile(
    r"\b(?:return|get back|come home)\s+from\s+(?P<city>[a-z][a-z .'-]{1,30})",
    re.I,
)
_HAS_OUTBOUND_ROUTE_RE = re.compile(
    r"从.{1,20}?(?:去|到|至|出发)|"
    r"(?:^|[，,。；;])[^，,。；;]{0,20}(?:去|到|至)[^，,。；;]{1,20}|"
    r"from\s+.+\s+to\s+|"
    r"\b[a-z][a-z .'-]{1,30}\s+to\s+[a-z][a-z .'-]{1,30}\b",
    re.I,
)
_RETURN_MENTION_RE = re.compile(
    r"返程|回程|返回|回来|往返|(?:当天|当日|下午|晚上|明天|后天).{0,6}回(?:$|[，,。；;])|"
    r"\breturn\b|\bround[ -]?trip\b|get back|come home|coming home|back by",
    re.I,
)
_EXPLICIT_RETURN_ONLY_RE = re.compile(
    r"^(?:只要|只订|仅)?(?:返程|回程)(?:\s|$|，|,|。)|"
    r"\b(?:return[- ]only|inbound[- ]only)\b",
    re.I,
)
_EXPLICIT_ONE_WAY_RE = re.compile(
    r"单程|只要去程|只订去程|不返程|不要返程|\bone[ -]?way\b|\boutbound[- ]only\b",
    re.I,
)


def return_from_city_span(message: str) -> str | None:
    """返回纯返程表达中的实际离开城市原文。"""
    text = message or ""
    if _HAS_OUTBOUND_ROUTE_RE.search(text):
        return None
    match = _RETURN_FROM_CITY_RE.search(text) or _RETURN_FROM_CITY_EN_RE.search(text)
    if match is None:
        return None
    return (match.group("city") or "").strip() or None


def message_is_return_leg_only(message: str) -> bool:
    """消息只描述返程航段，而不是一份含去程的往返请求。"""
    text = message or ""
    if _HAS_OUTBOUND_ROUTE_RE.search(text):
        return False
    return bool(
        return_from_city_span(text)
        or _EXPLICIT_RETURN_ONLY_RE.search(text)
        or _RETURN_MENTION_RE.search(text)
    )


def infer_booking_scope(
    message: str,
    *,
    prior_fields: Mapping[str, Any] | None = None,
) -> BookingScope:
    """从本轮消息和已确认行程推导本次预订范围。"""
    prior = prior_fields or {}
    prior_scope = _scope_value(prior.get("booking_scope"))
    has_prior_route = any(
        prior.get(name) not in (None, "")
        for name in ("origin", "destination", "departure_after", "arrive_by")
    )
    text = message or ""

    if _EXPLICIT_ONE_WAY_RE.search(text):
        return BookingScope.OUTBOUND_ONLY
    if _EXPLICIT_RETURN_ONLY_RE.search(text):
        return BookingScope.RETURN_ONLY
    if message_is_return_leg_only(text):
        # 已有往返任务中的“返程改到周三”是修订第二航段，不应把整单降成纯返程。
        if has_prior_route and prior_scope is not None:
            return prior_scope
        return BookingScope.RETURN_ONLY
    if _RETURN_MENTION_RE.search(text):
        return BookingScope.ROUND_TRIP
    if prior_scope is not None:
        return prior_scope
    return BookingScope.OUTBOUND_ONLY


def booking_scope_for_fields(
    fields: Mapping[str, Any],
    *,
    user_message: str = "",
) -> BookingScope:
    """读取显式范围；旧字段没有范围时按返程窗口或消息兼容推导。"""
    explicit = _scope_value(fields.get("booking_scope"))
    if explicit is not None:
        return explicit
    if fields.get("return_after") is not None or fields.get("return_before") is not None:
        if message_is_return_leg_only(user_message):
            return BookingScope.RETURN_ONLY
        return BookingScope.ROUND_TRIP
    return infer_booking_scope(user_message, prior_fields=fields)


def normalize_journey_fields(
    fields: Mapping[str, Any],
    *,
    user_message: str,
    provenance: Mapping[str, str] | None = None,
    return_from_city: str | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """按预订范围统一城市角色和时间槽，纯返程最终只保留一个真实方向航段。"""
    updated = dict(fields)
    prov = dict(provenance or {})
    notes: list[str] = []
    scope = booking_scope_for_fields(updated, user_message=user_message)
    updated["booking_scope"] = scope.value

    if scope is not BookingScope.RETURN_ONLY:
        return updated, notes, prov

    city = return_from_city
    origin = updated.get("origin") if isinstance(updated.get("origin"), str) else None
    destination = (
        updated.get("destination") if isinstance(updated.get("destination"), str) else None
    )
    origin_match = bool(city and origin and origin.casefold() == city.casefold())
    destination_match = bool(
        city and destination and destination.casefold() == city.casefold()
    )

    if city and destination_match:
        # 兼容旧“去程归一化”输出：home→X 要翻成当前真实返程航段 X→home。
        old_origin_provenance = prov.get("origin", "journey_semantics")
        updated["origin"] = city
        prov["origin"] = "journey_semantics"
        if origin and not origin_match:
            updated["destination"] = origin
            prov["destination"] = old_origin_provenance
            notes.append(f"return_only:route_reoriented={city}->{origin}")
        else:
            updated["destination"] = None
            prov.pop("destination", None)
            notes.append(f"return_only:origin={city};destination_missing")
    elif city and origin is None:
        updated["origin"] = city
        prov["origin"] = "journey_semantics"
        notes.append(f"return_only:origin={city}")

    return_after = updated.get("return_after")
    return_before = updated.get("return_before")
    if return_after is not None or return_before is not None:
        # 旧模型可能同时返回两套时间；纯返程时 return_* 才是与返程证据绑定的那一套。
        updated["departure_after"] = return_after
        updated["arrive_by"] = return_before
        if return_after is not None:
            prov["departure_after"] = prov.get("return_after", "journey_semantics")
            notes.append("return_only:return_after_moved_to_departure_after")
        else:
            prov.pop("departure_after", None)
        if return_before is not None:
            prov["arrive_by"] = prov.get("return_before", "journey_semantics")
            notes.append("return_only:return_before_moved_to_arrive_by")
        else:
            prov.pop("arrive_by", None)
    if return_after is not None or return_before is not None:
        updated["return_after"] = None
        updated["return_before"] = None
        prov.pop("return_after", None)
        prov.pop("return_before", None)

    return updated, notes, prov


def _scope_value(value: object) -> BookingScope | None:
    if isinstance(value, BookingScope):
        return value
    if isinstance(value, str):
        try:
            return BookingScope(value)
        except ValueError:
            return None
    return None
