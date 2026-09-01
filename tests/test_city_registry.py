"""城市登记表的守门人。

**这个文件存在的理由，就是让第 04 步不再发生一次。**

在登记表之前，"一座城市叫什么、在哪个时区、机票怎么查、酒店怎么查"分散在五个地方
各存一份。加一座城市要改五处，漏一处就是一种新的坏法，而**没有任何东西会因此变红**。
实测（`reports/evaluation-runs/city-coverage-*/`）：配置里正式声明支持的 Dubai 与
Sydney 订得到机票、订不到酒店，这条差异在仓库里躺了不知道多久。

所以这里断言的不是"表里有多少城市"（那会随业务变），而是**几条不该被打破的关系**。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from corporate_travel_agent.providers.duffel import DEFAULT_LOCATION_CODES
from corporate_travel_agent.providers.liteapi import DEFAULT_LOCATIONS
from corporate_travel_agent.services.city_registry import city_registry
from corporate_travel_agent.services.locations import DEFAULT_CITY_TIMEZONES
from corporate_travel_agent.services.policy_config import load_policy_configuration

REPO = Path(__file__).resolve().parents[1]

#: 政策配置声明支持、但供应商那边**还订不到**的城市。
#:
#: 这不是"允许有洞"，是**把洞写在明处**：每加一行都要说清楚缺的是哪一边、为什么。
#: 空着是目标状态。要清掉一行，得先在供应商沙箱上验证那座城市真的搜得到
#: （`examples/probe_provider_city_coverage.py`），而不是往表里编一条定位——
#: 编进去只会把"搜不了"换成"搜不到还说不清为什么"。
#:
#: Dubai 与 Sydney 原本在这里，`reports/evaluation-runs/city-probe-final-*/`
#: 实测两边都搜得到之后清掉了。
KNOWN_GAPS: dict[str, str] = {}


class CityRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = city_registry()

    def test_the_registry_loads_and_every_city_has_a_real_timezone(self) -> None:
        """时区在加载时就校验——坏的 IANA 名不该留到运行期才炸。"""
        self.assertTrue(self.registry.cities)
        for city in self.registry.cities:
            self.assertTrue(city.timezone, f"{city.code} 没有时区")

    def test_every_airport_belongs_to_a_registered_city(self) -> None:
        """机场不能指向一座不存在的城市，否则"从希思罗走"会查不到任何东西。"""
        known = {city.code for city in self.registry.cities}
        for airport in self.registry.airports:
            self.assertIn(airport.city, known, f"机场 {airport.iata} 指向未登记城市")

    def test_one_alias_never_points_at_two_cities(self) -> None:
        """同一个写法不能既是这座城市又是那座城市——那样搜到哪全看谁先注册。"""
        seen: dict[str, str] = {}
        for city in self.registry.cities:
            for key in city.lookup_keys():
                previous = seen.get(key)
                self.assertIsNone(
                    previous,
                    f"别名 {key!r} 同时指向 {previous} 和 {city.canonical_name}",
                )
                seen[key] = city.canonical_name

    def test_what_the_policy_says_it_supports_can_actually_be_booked(self) -> None:
        """**这是最要紧的一条：配置不许声明自己做不到的事。**

        `config.cities` 是这套系统对外声明支持的城市。它和供应商的表对不上，
        就不是"用户说了个冷门地名"，是配置在说谎——而用户要到搜索失败才知道。
        """
        configuration = load_policy_configuration()
        by_name = {city.canonical_name: city for city in self.registry.cities}
        for declared in configuration.config.cities:
            name = declared.canonical_name
            record = by_name.get(name)
            self.assertIsNotNone(record, f"政策配置声明的 {name} 不在登记表里")
            assert record is not None
            if name in KNOWN_GAPS:
                self.assertFalse(
                    record.bookable,
                    f"{name} 已经订得到了——请把它从 KNOWN_GAPS 里删掉",
                )
                continue
            self.assertTrue(
                record.bookable,
                f"政策配置声明支持 {name}，但供应商那边订不到；"
                "要么补齐供应商定位，要么写进 KNOWN_GAPS 说明缺什么",
            )

    def test_every_city_with_a_nightly_cap_is_registered(self) -> None:
        """有夜费上限却不在登记表里的城市，等于给一座订不到的城市定了预算。"""
        payload = json.loads(
            (REPO / "config" / "travel-policy.json").read_text(encoding="utf-8")
        )
        declared = {city["code"] for city in payload["cities"]}
        for code in payload["policies"][0]["hotel_city_caps"]:
            self.assertIn(code, declared, f"{code} 有夜费上限但不在 config.cities 里")

    def test_the_derived_tables_agree_with_each_other(self) -> None:
        """四张下游表是同一份数据的四个视图，不该各说各话。

        凡是 Duffel 查得到的写法，时区表也必须查得到——否则那趟行程搜得到票，
        却会**悄悄按默认时区**解释它的起降时间。
        """
        for key in DEFAULT_LOCATION_CODES:
            self.assertIn(key, DEFAULT_CITY_TIMEZONES, f"{key!r} 有 IATA 码却没有时区")
        for key in DEFAULT_LOCATIONS:
            self.assertIn(key, DEFAULT_CITY_TIMEZONES, f"{key!r} 有酒店定位却没有时区")
            self.assertIn(
                key, DEFAULT_LOCATION_CODES, f"{key!r} 订得到酒店却订不到机票"
            )


if __name__ == "__main__":
    unittest.main()
