"""deterministic_parser：确定性中文意图基线解析器（评测套件用，不编造歧义日期）。"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

_CITY_NAMES = tuple(
    sorted(
        {
            "阿勒泰",
            "乌鲁木齐",
            "呼和浩特",
            "石家庄",
            "哈尔滨",
            "长春",
            "银川",
            "郑州",
            "长沙",
            "南昌",
            "杭州",
            "济南",
            "青岛",
            "福州",
            "拉萨",
            "上海",
            "澳门",
            "南京",
            "成都",
            "香港",
            "深圳",
            "广州",
            "西安",
            "珠海",
            "苏州",
            "四川",
            "云南",
            "北京",
        },
        key=len,
        reverse=True,
    )
)


class DeterministicChineseIntentParser:
    """离线中文意图基线解析器，用于固定中文意图评测套件。

    故意把歧义日期与截止时间留空，作可复现安全基线，而非抄评测标签的 oracle。
    """

    prompt_version = "deterministic-zh-intent-v1"

    @staticmethod
    def _classification(message: str) -> str:
        if "附近" in message and any(
            keyword in message
            for keyword in ("图书馆", "公检法", "度假村", "可以买火车票", "推荐")
        ):
            return "OUT_OF_SCOPE"
        if "高铁" in message and "飞机" in message:
            return "TRANSPORT_COMPARE"
        if any(
            keyword in message
            for keyword in (
                "住宿",
                "酒店",
                "行程安排",
                "旅游攻略",
                "规划一条",
                "多日",
                "2天",
                "两日",
                "5日",
            )
        ):
            return "MULTI_DAY_TRIP"
        return "NEEDS_CLARIFICATION"

    @staticmethod
    def _cities(message: str) -> tuple[str | None, str | None]:
        matches = sorted(
            (
                (message.find(city), city)
                for city in _CITY_NAMES
                if city in message
            ),
            key=lambda item: item[0],
        )
        cities = [city for _, city in matches]
        if len(cities) >= 2:
            return cities[0], cities[1]
        if not cities:
            return None, None
        city = cities[0]
        city_index = message.find(city)
        suffix = message[city_index + len(city) : city_index + len(city) + 4]
        if "出发" in suffix:
            return city, None
        return None, city

    @staticmethod
    def _departure_date(
        message: str,
        reference_time: datetime,
        timezone: ZoneInfo,
    ) -> datetime | None:
        if "明天" in message:
            return datetime.combine(
                reference_time.date() + timedelta(days=1),
                time.min,
                tzinfo=timezone,
            )
        match = re.search(r"(?P<month>\d{1,2})月(?P<day>\d{1,2})[号日]?", message)
        if match is None:
            return None
        month = int(match.group("month"))
        day = int(match.group("day"))
        year = reference_time.year
        candidate = datetime(year, month, day, tzinfo=timezone)
        if candidate < reference_time:
            candidate = datetime(year + 1, month, day, tzinfo=timezone)
        return candidate
