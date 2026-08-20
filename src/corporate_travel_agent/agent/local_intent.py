"""local_intent：有依据的本地行程意图解析器（L0）。从不编造城市。"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from corporate_travel_agent.services.locations import DEFAULT_CITY_TIMEZONES, CityNormalizer

from .ports import IntentExtractionResult, LLMCallMetadata
from .schemas import IntentExtractionSchema, TripIntentFields

_WEEKDAY_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_ROUTE_CN = re.compile(
    r"从(?P<origin>.{1,20}?)(?:出发)?(?:去|到|至)(?P<destination>.{1,20}?)(?:见|开会|出差|，|。|$)"
)
_ROUTE_EN = re.compile(
    r"\bfrom\s+(?P<origin>[a-z][a-z .'-]{1,30}?)\s+to\s+(?P<destination>[a-z][a-z .'-]{1,30}?)\b",
    re.I,
)
_WEEKDAY_CN = re.compile(r"(?P<scope>下下|下|本|这)?周(?P<day>[一二三四五六日天])")


class GroundedLocalIntentParser:
    """确定性双语抽取器：作 L0，亦作 LLM 故障回退。

    仅填充用户文本 + 已校验城市目录能支撑的槽；歧义日期留空，不猜测。
    """

    prompt_version = "grounded-local-intent-v1"

    def __init__(self, city_normalizer: CityNormalizer | None = None) -> None:
        self.city_normalizer = city_normalizer or CityNormalizer()
        self._catalog = self._build_catalog()

    def extract_trip_intent(
        self,
        message: str,
        *,
        task_id: str,
        traveler_id: str,
        context: dict[str, Any],
    ) -> IntentExtractionResult:
        """从用户消息抽取有依据的意图字段。"""
        _ = (task_id, traveler_id)
        started = monotonic()
        reference_time = datetime.fromisoformat(str(context["reference_time"]))
        timezone = ZoneInfo(str(context.get("timezone", "Asia/Shanghai")))
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone)
        else:
            reference_time = reference_time.astimezone(timezone)

        origin, destination = self._cities(message)
        from corporate_travel_agent.services.locations import resolve_location_timezone

        origin_tz = ZoneInfo(
            resolve_location_timezone(origin, fallback=timezone.key)
        )
        dest_tz = ZoneInfo(
            resolve_location_timezone(destination, fallback=origin_tz.key)
        )

        outbound_day = self._outbound_date(message, reference_time)
        return_day = self._return_date(message, reference_time, outbound_day)
        departure_after = (
            datetime.combine(outbound_day, time(8, 0), tzinfo=origin_tz)
            if outbound_day is not None
            else None
        )
        arrive_by = None
        if outbound_day is not None and re.search(
            r"开会|会议|抵达|到达|最晚|\d+\s*点|\bmeeting\b|\barrive\b",
            message,
            re.I,
        ):
            arrive_by = datetime.combine(outbound_day, time(18, 0), tzinfo=dest_tz)
        evening_return = bool(re.search(r"晚上|今晚|tonight", message, re.I))
        return_after = (
            datetime.combine(
                return_day,
                time(18, 0) if evening_return else time(13, 0),
                tzinfo=dest_tz,
            )
            if return_day is not None
            else None
        )
        return_before = (
            datetime.combine(
                return_day,
                time(23, 0) if evening_return else time(22, 0),
                tzinfo=dest_tz,
            )
            if return_day is not None
            else None
        )

        hard: list[str] = []
        soft: list[str] = []
        if re.search(
            r"compare.{0,24}(train|flight|高铁|飞机)|(train|高铁).{0,16}(and|or|和|与|还是).{0,16}(flight|飞机)|(flight|飞机).{0,16}(and|or|和|与|还是).{0,16}(train|高铁)",
            message,
            re.I,
        ):
            soft.append("compare_train_and_flight")
        if re.search(
            r"优先飞机|只要飞机|prefer(?:\s+the)?\s+flight|\bflight only\b",
            message,
            re.I,
        ):
            soft.append("prefer_flight")
        if re.search(
            r"优先高铁|优先火车|prefer(?:\s+the)?\s+train|\btrain only\b",
            message,
            re.I,
        ):
            soft.append("prefer_train")
        if re.search(
            r"离客户|靠近客户|客户公司附近|客户附近|方便去客户|"
            r"near(?:\s+the)?\s+client|close to (?:the )?client",
            message,
            re.I,
        ):
            soft.append("hotel_near_client")

        classification = self._classification(message, origin, destination)
        fields = TripIntentFields(
            origin=origin,
            destination=destination,
            departure_after=departure_after,
            arrive_by=arrive_by,
            return_after=return_after,
            return_before=return_before,
            hotel_check_in=None,
            hotel_check_out=None,
            client_location=None,
            hard_constraints=hard,  # type: ignore[arg-type]
            soft_preferences=soft,  # type: ignore[arg-type]
        )
        provided = [
            name
            for name, value in fields.model_dump().items()
            if value not in (None, [])
        ]
        payload = IntentExtractionSchema(
            classification=classification,  # type: ignore[arg-type]
            fields=fields,
            provided_fields=provided,  # type: ignore[arg-type]
            missing_required_fields=[],
            conflicts=[],
            assumptions=["local_parser:grounded_slots_only"],
            confidence=0.8 if origin and destination else 0.45,
            manipulation_detected=False,
        )
        return IntentExtractionResult(
            payload=payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="grounded-local-parser",
                duration_ms=int((monotonic() - started) * 1000),
            ),
        )

    def _build_catalog(self) -> list[tuple[str, str]]:
        aliases: dict[str, str] = {}
        for alias, canonical in self.city_normalizer.alias_map().items():
            aliases[alias.casefold()] = canonical
        for raw in DEFAULT_CITY_TIMEZONES:
            key = raw.casefold()
            aliases.setdefault(key, self.city_normalizer.canonicalize(raw))
        return sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)

    def _mentions_are_alternatives(self, message: str) -> bool:
        """两城是 OR 二选一而非路线时为 True。"""
        if _ROUTE_CN.search(message) or _ROUTE_EN.search(message):
            return False
        mentions = self._city_mentions(message)
        unique: list[tuple[int, str]] = []
        seen: set[str] = set()
        for index, canonical in mentions:
            if canonical in seen:
                continue
            seen.add(canonical)
            unique.append((index, canonical))
        if len(unique) < 2:
            return False
        start, end = unique[0][0], unique[1][0]
        lo, hi = (start, end) if start <= end else (end, start)
        between = message[lo:hi + 12]
        return bool(
            re.search(
                r"或者|还是|或是|要么|(?<![a-z])or(?![a-z])|either",
                between,
                re.I,
            )
        )

    def _cities(self, message: str) -> tuple[str | None, str | None]:
        """解析出发/目的；不确定则返回 None。"""
        mentions = self._city_mentions(message)
        if self._mentions_are_alternatives(message):
            return None, None
        route = _ROUTE_CN.search(message) or _ROUTE_EN.search(message)
        if route is not None:
            origin = self._lookup_span(route.group("origin"))
            destination = self._lookup_span(route.group("destination"))
            if origin or destination:
                return origin, destination
        unique: list[str] = []
        for _, canonical in mentions:
            if canonical not in unique:
                unique.append(canonical)
        if len(unique) >= 2:
            return unique[0], unique[1]
        if len(unique) == 1:
            city = unique[0]
            index = mentions[0][0]
            window = message[max(0, index - 2) : index + 8]
            if "从" in window or "出发" in window:
                return city, None
            return None, city
        return None, None

    def _city_mentions(self, message: str) -> list[tuple[int, str]]:
        """按出现位置返回 (index, canonical) 城市提及。"""
        text = message
        folded = message.casefold()
        occupied = [False] * len(text)
        found: list[tuple[int, str]] = []
        for alias, canonical in self._catalog:
            if not alias:
                continue
            if any("a" <= char <= "z" for char in alias):
                pattern = re.compile(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", re.I)
                for match in pattern.finditer(folded):
                    start, end = match.start(), match.end()
                    if any(occupied[start:end]):
                        continue
                    for index in range(start, end):
                        occupied[index] = True
                    found.append((start, canonical))
            else:
                start = 0
                while True:
                    index = text.find(alias, start)
                    if index < 0:
                        break
                    end = index + len(alias)
                    if not any(occupied[index:end]):
                        for pos in range(index, end):
                            occupied[pos] = True
                        found.append((index, canonical))
                    start = index + 1
        found.sort(key=lambda item: item[0])
        return found

    def _lookup_span(self, span: str | None) -> str | None:
        if not span:
            return None
        mentions = self._city_mentions(span.strip())
        if not mentions:
            return None
        return mentions[0][1]

    def _outbound_date(self, message: str, reference: datetime) -> date | None:
        """解析去程日；歧义则 None。"""
        from corporate_travel_agent.agent.intent_calibration import (
            iter_calendar_date_mentions,
            message_has_ambiguous_weekday_choice,
            message_is_lunar_or_holiday_without_gregorian,
            message_is_return_scoped_relative_day,
            resolve_calendar_mention,
        )

        mentions = iter_calendar_date_mentions(message)
        if mentions:
            return resolve_calendar_mention(mentions[0], reference_time=reference)
        if message_is_lunar_or_holiday_without_gregorian(message):
            return None
        if message_has_ambiguous_weekday_choice(message):
            return None
        if not message_is_return_scoped_relative_day(message):
            if "明天" in message or re.search(r"\btomorrow\b", message, re.I):
                return reference.date() + timedelta(days=1)
            if "后天" in message:
                return reference.date() + timedelta(days=2)
        weekday = self._first_weekday(message)
        if weekday is None:
            return None
        if weekday[0] == "下下":
            return _weekday_in_week_after_next(reference.date(), weekday[1])
        if weekday[0] in {"下"}:
            return _weekday_in_next_week(reference.date(), weekday[1])
        return _next_or_same_weekday(reference.date(), weekday[1])

    def _return_date(
        self,
        message: str,
        reference: datetime,
        outbound: date | None,
    ) -> date | None:
        """解析返程日；未提返程则 None。"""
        if not re.search(
            r"回来|返回|返程|回程|下午回|晚上回|当天回|\d{1,2}[日号]回|\breturn\b",
            message,
        ):
            return None
        from corporate_travel_agent.agent.intent_calibration import (
            iter_calendar_date_mentions,
            resolve_calendar_mention,
        )

        mentions = iter_calendar_date_mentions(message)
        if len(mentions) >= 2:
            return resolve_calendar_mention(
                mentions[1],
                reference_time=reference,
                min_day=outbound,
            )
        weekdays = list(_WEEKDAY_CN.finditer(message))
        if outbound is not None and len(weekdays) >= 2:
            return _on_or_after_weekday(
                outbound, _WEEKDAY_INDEX[weekdays[1].group("day")]
            )
        from corporate_travel_agent.agent.intent_calibration import (
            message_is_return_scoped_relative_day,
        )

        if message_is_return_scoped_relative_day(message):
            if "后天" in message:
                return reference.date() + timedelta(days=2)
            if "明天" in message or re.search(r"\btomorrow\b", message, re.I):
                return reference.date() + timedelta(days=1)
        return None

    @staticmethod
    def _first_weekday(message: str) -> tuple[str | None, int] | None:
        match = _WEEKDAY_CN.search(message)
        if match is None:
            return None
        return match.group("scope"), _WEEKDAY_INDEX[match.group("day")]

    @staticmethod
    def _classification(
        message: str,
        origin: str | None,
        destination: str | None,
    ) -> str:
        """粗分 TRIP / MULTI_DAY / NEEDS_CLARIFICATION / OUT_OF_SCOPE。"""
        if re.search(r"附近", message) and any(
            token in message for token in ("图书馆", "公检法", "度假村")
        ):
            return "OUT_OF_SCOPE"
        if re.search(r"酒店|住宿|回来|返程|往返|overnight|hotel", message, re.I):
            return "MULTI_DAY_TRIP"
        if origin and destination:
            return "TRIP"
        return "NEEDS_CLARIFICATION"


def _weekday_in_next_week(today: date, weekday: int) -> date:
    days_to_monday = (7 - today.weekday()) % 7
    if days_to_monday == 0:
        days_to_monday = 7
    return today + timedelta(days=days_to_monday + weekday)


def _weekday_in_week_after_next(today: date, weekday: int) -> date:
    return _weekday_in_next_week(today, weekday) + timedelta(days=7)


def _next_or_same_weekday(today: date, weekday: int) -> date:
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta)


def _on_or_after_weekday(start: date, weekday: int) -> date:
    delta = (weekday - start.weekday()) % 7
    return start + timedelta(days=delta)
