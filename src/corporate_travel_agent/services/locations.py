"""城市/机场标签归一化与 IANA 时区解析（未知地点 fail-closed 回退）。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 已校验的城市/机场/都市圈标签 → IANA 时区。
# Fail-closed：未知地点保留调用方提供的回退（通常为策略默认时区）。
DEFAULT_CITY_TIMEZONES: dict[str, str] = {
    # --- Greater China / East Asia ---
    "beijing": "Asia/Shanghai",
    "北京": "Asia/Shanghai",
    "北京市": "Asia/Shanghai",
    "bjs": "Asia/Shanghai",
    "pek": "Asia/Shanghai",
    "cn-bjs": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "上海市": "Asia/Shanghai",
    "sha": "Asia/Shanghai",
    "pvg": "Asia/Shanghai",
    "cn-sha": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong",
    "hongkong": "Asia/Hong_Kong",
    "香港": "Asia/Hong_Kong",
    "hkg": "Asia/Hong_Kong",
    "hk-hkg": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei",
    "台北": "Asia/Taipei",
    "臺北": "Asia/Taipei",
    "tpe": "Asia/Taipei",
    "tokyo": "Asia/Tokyo",
    "东京": "Asia/Tokyo",
    "東京": "Asia/Tokyo",
    "tyo": "Asia/Tokyo",
    "nrt": "Asia/Tokyo",
    "hnd": "Asia/Tokyo",
    "jp-tyo": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "大阪": "Asia/Tokyo",
    "kix": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "首尔": "Asia/Seoul",
    "首爾": "Asia/Seoul",
    "icn": "Asia/Seoul",
    "sel": "Asia/Seoul",
    "singapore": "Asia/Singapore",
    "新加坡": "Asia/Singapore",
    "sin": "Asia/Singapore",
    "sg-sin": "Asia/Singapore",
    "bangkok": "Asia/Bangkok",
    "曼谷": "Asia/Bangkok",
    "bkk": "Asia/Bangkok",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "吉隆坡": "Asia/Kuala_Lumpur",
    "kul": "Asia/Kuala_Lumpur",
    # --- United States / Canada ---
    "new york": "America/New_York",
    "newyork": "America/New_York",
    "nyc": "America/New_York",
    "纽约": "America/New_York",
    "紐約": "America/New_York",
    "jfk": "America/New_York",
    "lga": "America/New_York",
    "ewr": "America/New_York",
    "us-nyc": "America/New_York",
    "philadelphia": "America/New_York",
    "philly": "America/New_York",
    "费城": "America/New_York",
    "費城": "America/New_York",
    "phl": "America/New_York",
    "us-phl": "America/New_York",
    "boston": "America/New_York",
    "波士顿": "America/New_York",
    "波士頓": "America/New_York",
    "bos": "America/New_York",
    "us-bos": "America/New_York",
    "washington": "America/New_York",
    "washington dc": "America/New_York",
    "washington d.c.": "America/New_York",
    "华盛顿": "America/New_York",
    "華盛頓": "America/New_York",
    "was": "America/New_York",
    "iad": "America/New_York",
    "dca": "America/New_York",
    "bwi": "America/New_York",
    "us-was": "America/New_York",
    "miami": "America/New_York",
    "迈阿密": "America/New_York",
    "邁阿密": "America/New_York",
    "mia": "America/New_York",
    "us-mia": "America/New_York",
    "orlando": "America/New_York",
    "奥兰多": "America/New_York",
    "mco": "America/New_York",
    "atlanta": "America/New_York",
    "亚特兰大": "America/New_York",
    "亞特蘭大": "America/New_York",
    "atl": "America/New_York",
    "detroit": "America/Detroit",
    "底特律": "America/Detroit",
    "dtw": "America/Detroit",
    "chicago": "America/Chicago",
    "芝加哥": "America/Chicago",
    "chi": "America/Chicago",
    "ord": "America/Chicago",
    "mdw": "America/Chicago",
    "us-chi": "America/Chicago",
    "dallas": "America/Chicago",
    "达拉斯": "America/Chicago",
    "達拉斯": "America/Chicago",
    "dfw": "America/Chicago",
    "houston": "America/Chicago",
    "休斯顿": "America/Chicago",
    "休斯頓": "America/Chicago",
    "iah": "America/Chicago",
    "minneapolis": "America/Chicago",
    "msp": "America/Chicago",
    "denver": "America/Denver",
    "丹佛": "America/Denver",
    "den": "America/Denver",
    "phoenix": "America/Phoenix",
    "凤凰城": "America/Phoenix",
    "鳳凰城": "America/Phoenix",
    "phx": "America/Phoenix",
    "los angeles": "America/Los_Angeles",
    "losangeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "洛杉矶": "America/Los_Angeles",
    "洛杉磯": "America/Los_Angeles",
    "lax": "America/Los_Angeles",
    "us-lax": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "sanfrancisco": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "旧金山": "America/Los_Angeles",
    "三藩市": "America/Los_Angeles",
    "sfo": "America/Los_Angeles",
    "us-sfo": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "西雅图": "America/Los_Angeles",
    "西雅圖": "America/Los_Angeles",
    "sea": "America/Los_Angeles",
    "us-sea": "America/Los_Angeles",
    "san diego": "America/Los_Angeles",
    "圣地亚哥": "America/Los_Angeles",
    "san": "America/Los_Angeles",
    "las vegas": "America/Los_Angeles",
    "拉斯维加斯": "America/Los_Angeles",
    "拉斯維加斯": "America/Los_Angeles",
    "las": "America/Los_Angeles",
    "toronto": "America/Toronto",
    "多伦多": "America/Toronto",
    "多倫多": "America/Toronto",
    "yto": "America/Toronto",
    "yyz": "America/Toronto",
    "ca-yto": "America/Toronto",
    "vancouver": "America/Vancouver",
    "温哥华": "America/Vancouver",
    "溫哥華": "America/Vancouver",
    "yvr": "America/Vancouver",
    "montreal": "America/Toronto",
    "蒙特利尔": "America/Toronto",
    "ymq": "America/Toronto",
    "yul": "America/Toronto",
    # --- Europe ---
    "london": "Europe/London",
    "伦敦": "Europe/London",
    "倫敦": "Europe/London",
    "lon": "Europe/London",
    "lhr": "Europe/London",
    "lgw": "Europe/London",
    "stn": "Europe/London",
    "gb-lon": "Europe/London",
    "paris": "Europe/Paris",
    "巴黎": "Europe/Paris",
    "par": "Europe/Paris",
    "cdg": "Europe/Paris",
    "ory": "Europe/Paris",
    "fr-par": "Europe/Paris",
    "frankfurt": "Europe/Berlin",
    "法兰克福": "Europe/Berlin",
    "法蘭克福": "Europe/Berlin",
    "fra": "Europe/Berlin",
    "de-fra": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "阿姆斯特丹": "Europe/Amsterdam",
    "ams": "Europe/Amsterdam",
    "nl-ams": "Europe/Amsterdam",
    # --- Middle East / Oceania ---
    "dubai": "Asia/Dubai",
    "迪拜": "Asia/Dubai",
    "dxb": "Asia/Dubai",
    "ae-dxb": "Asia/Dubai",
    "sydney": "Australia/Sydney",
    "悉尼": "Australia/Sydney",
    "syd": "Australia/Sydney",
    "au-syd": "Australia/Sydney",
}

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
