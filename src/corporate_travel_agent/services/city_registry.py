"""城市登记表：**一座城市的地理事实只存一份**。

## 为什么要有这个文件

在它之前，"这座城市叫什么、在哪个时区、机票怎么查、酒店怎么查"分散在**五个地方**
各存一份，互不相认：

| 存在哪 | 存的是什么 | 查不到时会怎样 |
|---|---|---|
| `config/travel-policy.json` 的 `cities` | 别名 → 规范名 | **原话原样往下传**，不报错 |
| 同一份文件的 `hotel_city_caps` | 城市 → 夜费上限 | 政策落"证据不足"，方案被降档 |
| `providers/duffel.py` | 别名 → IATA 码 | ProviderError，搜不了机票 |
| `providers/liteapi.py` | 别名 → 供应商定位 | ProviderError，搜不了酒店 |
| `services/locations.py` | 别名 → IANA 时区 | **悄悄回退到默认时区** |

实测（`reports/evaluation-runs/city-coverage-*/`）：这个仓库自己的数据集与红队用例里
出现过的 20 个中文城市名，**只有 7 个真的搜得到票**；配置里正式声明支持的 Dubai 与
Sydney **订得到机票、订不到酒店**。加一座城市要改五个地方，漏一个就是一种新的坏法。

## 这里存什么、不存什么

**只存地理事实**：城市叫什么、有哪些别名、在哪个时区、供应商怎么称呼它。
**不存政策决定**——夜费上限属于政策快照，它是公司定的，不是地理属性，
换一份政策就换一套上限。两者混在一起的话，改政策会变成改地理。

`liteapi` 为 ``null`` 表示**这家供应商那边还没验证过这座城市**，不是"没写"。
留空是诚实的：LiteAPI 查不到会 fail-closed 报错，而编一条进去会让系统
拿着没验证过的定位去搜，搜不到还说不清为什么。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_REGISTRY_FILE = (
    Path(__file__).resolve().parents[3] / "config" / "cities.json"
)


@dataclass(frozen=True, slots=True)
class CityRecord:
    """一座城市的全部地理事实。"""

    code: str
    canonical_name: str
    aliases: tuple[str, ...]
    timezone: str
    duffel_code: str | None
    liteapi: Mapping[str, str] | None

    @property
    def bookable(self) -> bool:
        """机票和酒店**都**查得到才算订得了。

        少一边不是"部分可用"：只有机票没有酒店，一趟要住店的差旅照样出不来方案。
        """
        return self.duffel_code is not None and self.liteapi is not None

    def lookup_keys(self) -> tuple[str, ...]:
        """这座城市能被哪些写法查到。

        包含规范名、全部别名、城市码（``CN-SHA``）以及城市码后缀（``sha``）——
        最后两种是政策配置里的写法，`_lookup_keys` 一直在按它们查。
        """
        keys = [self.canonical_name, *self.aliases, self.code]
        if "-" in self.code:
            keys.append(self.code.split("-", 1)[1])
        if self.duffel_code:
            keys.append(self.duffel_code)
        return tuple(dict.fromkeys(item.strip().casefold() for item in keys if item))


@dataclass(frozen=True, slots=True)
class AirportRecord:
    """一个机场：属于哪座城市、在哪个时区。

    机场码**不折叠成城市**：`LHR` 不能变成 `London` 再变成 `LON`，
    那会把"从希思罗走"改成"从伦敦任一机场走"。
    """

    iata: str
    city: str
    timezone: str


@dataclass(frozen=True, slots=True)
class CityRegistry:
    """全部城市与机场。四张下游表都从这里派生，不再各存一份。"""

    cities: tuple[CityRecord, ...]
    airports: tuple[AirportRecord, ...]

    def by_code(self) -> dict[str, CityRecord]:
        return {city.code: city for city in self.cities}

    def airport_keys(self, airport: AirportRecord) -> tuple[str, ...]:
        """这个机场能被哪些写法查到：三字母码本身，以及"城市名 + 码"。

        "从伦敦希思罗走"在英文里就说成 ``london lhr``。此前这几条是**逐个手写**
        进两张表的（只写了 LHR、JFK、LAX、SFO 四个），现在每个机场都自动有。
        """
        keys = [airport.iata]
        city = self.by_code().get(airport.city)
        if city is not None:
            keys.append(f"{city.canonical_name} {airport.iata}")
        return tuple(item.strip().casefold() for item in keys)

    def alias_map(self) -> dict[str, str]:
        """别名 → 规范名。喂给 `CityNormalizer`。"""
        table: dict[str, str] = {}
        for city in self.cities:
            for key in city.lookup_keys():
                table.setdefault(key, city.canonical_name)
        return table

    def timezone_map(self) -> dict[str, str]:
        """别名 → IANA 时区。机场也在里面，各按自己所在地。"""
        table: dict[str, str] = {}
        for city in self.cities:
            for key in city.lookup_keys():
                table.setdefault(key, city.timezone)
        for airport in self.airports:
            for key in self.airport_keys(airport):
                table[key] = airport.timezone
        return table

    def duffel_codes(self) -> dict[str, str]:
        """别名 → Duffel 用的 IATA 码。机场码指向它自己。"""
        table: dict[str, str] = {}
        for city in self.cities:
            if city.duffel_code is None:
                continue
            for key in city.lookup_keys():
                table.setdefault(key, city.duffel_code)
        for airport in self.airports:
            for key in self.airport_keys(airport):
                table[key] = airport.iata
        return table

    def liteapi_locations(self) -> dict[str, dict[str, str]]:
        """别名 → LiteAPI 定位。**只收已验证过的城市**，没验证的一律不出现。

        机场按机场查（``{"iataCode": ...}``），不折叠成所在城市——
        说"希思罗附近"和说"伦敦"要的不是同一批酒店。只有城市本身已验证过的机场
        才收进来：城市都没验证，机场更没有。
        """
        table: dict[str, dict[str, str]] = {}
        verified: set[str] = set()
        for city in self.cities:
            if city.liteapi is None:
                continue
            verified.add(city.code)
            for key in city.lookup_keys():
                table.setdefault(key, dict(city.liteapi))
        for airport in self.airports:
            if airport.city not in verified:
                continue
            for key in self.airport_keys(airport):
                table[key] = {"iataCode": airport.iata}
        return table


def _validated_timezone(name: str, *, where: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{where} 的时区 {name!r} 不是有效的 IANA 名") from exc
    return name


def load_city_registry(path: Path | None = None) -> CityRegistry:
    """从 ``config/cities.json`` 读登记表；时区在这里就校验，不留到运行期。"""
    source = path or _REGISTRY_FILE
    payload = json.loads(source.read_text(encoding="utf-8"))
    cities = tuple(
        CityRecord(
            code=item["code"],
            canonical_name=item["canonical_name"],
            aliases=tuple(item.get("aliases") or ()),
            timezone=_validated_timezone(item["timezone"], where=item["code"]),
            duffel_code=item.get("duffel_code"),
            liteapi=item.get("liteapi"),
        )
        for item in payload["cities"]
    )
    known = {city.code for city in cities}
    airports: list[AirportRecord] = []
    for item in payload.get("airports") or ():
        if item["city"] not in known:
            raise ValueError(f"机场 {item['iata']} 指向未登记的城市 {item['city']}")
        airports.append(
            AirportRecord(
                iata=item["iata"],
                city=item["city"],
                timezone=_validated_timezone(item["timezone"], where=item["iata"]),
            )
        )
    duplicate = len(cities) - len({city.code for city in cities})
    if duplicate:
        raise ValueError("城市码重复")
    return CityRegistry(cities=cities, airports=tuple(airports))


@lru_cache(maxsize=1)
def city_registry() -> CityRegistry:
    """进程内共用的一份登记表。"""
    return load_city_registry()
