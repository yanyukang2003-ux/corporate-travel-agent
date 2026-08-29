"""城市/机场标签归一化与 IANA 时区解析（未知地点 fail-closed 回退）。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from corporate_travel_agent.services.city_registry import city_registry

#: 已校验的城市/机场/都市圈标签 → IANA 时区。
#: **由城市登记表派生，不再在这里手写一份**——同一座城市的时区此前在这个文件、
#: Duffel 的 IATA 表、LiteAPI 的定位表、政策配置的别名表里各存一份，谁也不认谁。
#: Fail-closed：未知地点保留调用方提供的回退（通常为策略默认时区）。
DEFAULT_CITY_TIMEZONES: dict[str, str] = city_registry().timezone_map()


_IATA_LOCATION_CODE = re.compile(r"^[A-Za-z]{3}$")


class CityNormalizer:
    """将用户侧城市别名映射为供应商侧确定性规范名。"""

    def __init__(self, aliases: Mapping[str, str] | None = None) -> None:
        source = aliases or {}
        self._aliases = {
            alias.strip().casefold(): canonical.strip()
            for alias, canonical in source.items()
        }

    def canonicalize(self, value: str) -> str:
        """规范化城市/机场字符串；三位 IATA 码原样大写保留。"""
        normalized = value.strip()
        # 三位 IATA 携带机场/都市圈语义，不能折叠成城市展示名
        # （例如 LHR→London→LON，或 PEK→Beijing→BJS）。
        if _IATA_LOCATION_CODE.fullmatch(normalized):
            return normalized.upper()
        return self._aliases.get(normalized.casefold(), normalized)

    def alias_map(self) -> dict[str, str]:
        """返回当前别名表副本。"""
        return dict(self._aliases)


def resolve_location_timezone(
    location: str | None,
    *,
    fallback: str = "Asia/Shanghai",
    extra: Mapping[str, str] | None = None,
) -> str:
    """解析城市/机场标签对应的 IANA 时区。

    未知地点返回 fallback 且不抛错；无效的 fallback 会抛 ValueError，
    避免静默使用错误时区。
    """
    fallback_name = _validated_timezone_name(fallback, field_name="fallback")
    if location is None:
        return fallback_name
    text = str(location).strip()
    if not text:
        return fallback_name

    keys = _lookup_keys(text)
    table = dict(DEFAULT_CITY_TIMEZONES)
    if extra:
        for key, value in extra.items():
            table[str(key).strip().casefold()] = _validated_timezone_name(
                value, field_name=f"extra[{key!r}]"
            )
    for key in keys:
        match = table.get(key)
        if match is not None:
            return _validated_timezone_name(match, field_name="location timezone")
    return fallback_name


def route_timezones(
    origin: str | None,
    destination: str | None,
    *,
    fallback: str = "Asia/Shanghai",
    extra: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """返回 (出发地时区, 目的地时区)；目的地依次回退到出发地时区再回退到策略默认。"""
    origin_tz = resolve_location_timezone(origin, fallback=fallback, extra=extra)
    destination_tz = resolve_location_timezone(
        destination,
        fallback=origin_tz,
        extra=extra,
    )
    return origin_tz, destination_tz


def _lookup_keys(location: str) -> tuple[str, ...]:
    stripped = location.strip()
    folded = stripped.casefold()
    compact = "".join(folded.split())
    keys = [folded, compact]
    # "New York JFK" → try full phrase then trailing airport token.
    parts = folded.split()
    if len(parts) >= 2:
        keys.append(parts[-1])
        keys.append(" ".join(parts[:-1]))
    # Policy city codes such as US-NYC.
    if "-" in folded:
        keys.append(folded)
        keys.append(folded.split("-", 1)[-1])
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return tuple(ordered)


def _validated_timezone_name(value: str, *, field_name: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError(f"{field_name} must be a non-empty IANA timezone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid IANA timezone") from exc
    return name
