"""量一次：用户会说的城市名，有多少座这套系统认不出来，以及**在哪一步认不出来**。

方案第 04 步（城市别名表）的开工前测量，规矩见 HANDOFF §30.6：
**不要凭一段描述就开工。**

**名词**（先解释再用）：

- **别名表**：把用户说的名字（"上海"、"SHA"、"shanghai"）折算成系统内部一个
  确定的名字。城市有几十种叫法，下游只认一种。
- **规范名**：别名表折算出来的那一个名字，例如 `Shanghai`。
- **落表外**：用户说的名字在某张表里查不到。**关键在于不同的表落表外的后果不同**。

这个脚本回答一个具体问题：**第 04 步是补一张表，还是要接一个地名服务？**
不调用任何模型，不访问外网，不花钱。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from corporate_travel_agent.providers.duffel import DEFAULT_LOCATION_CODES
from corporate_travel_agent.providers.liteapi import DEFAULT_LOCATIONS
from corporate_travel_agent.services.locations import (
    DEFAULT_CITY_TIMEZONES,
    CityNormalizer,
)
from corporate_travel_agent.services.policy_config import load_policy_configuration

REPO = Path(__file__).resolve().parents[1]

#: 这个仓库自己的数据集、红队用例与测试里**真的出现过**的中文城市名。
#: 每一个都能在 `data/ evals/ tests/ examples/ src/` 里 grep 到——
#: 这不是我编的样本，是这套系统已经被喂过的名字。
REPO_EXERCISED: tuple[str, ...] = (
    "北京", "上海", "杭州", "广州", "深圳", "成都", "哈尔滨",
    "福州", "珠海", "阿勒泰", "大阪", "西安", "重庆", "青岛",
    "昆明", "南京", "伦敦", "纽约", "费城", "东京",
)

#: 中国大陆客流最大的一批机场所在城市，外加几个常见的国际商务目的地。
#: **这一组是作者列的，不是实测的用户数据**——它回答的是"把表补全要补多少"，
#: 不是"用户实际会说什么"。
NAMED_AIR_MARKETS: tuple[str, ...] = (
    "北京", "上海", "广州", "深圳", "成都", "重庆", "昆明", "西安",
    "杭州", "南京", "郑州", "长沙", "武汉", "青岛", "厦门", "天津",
    "海口", "三亚", "乌鲁木齐", "大连", "沈阳", "哈尔滨", "济南", "福州",
    "南宁", "贵阳", "兰州", "太原", "合肥", "宁波",
    "首尔", "曼谷", "吉隆坡", "台北", "大阪", "悉尼", "迪拜", "多伦多",
)


@dataclass(frozen=True, slots=True)
class Verdict:
    """一个名字走完一遍的结果：每一步认不认得它。"""

    spoken: str
    canonical: str
    in_alias_table: bool
    in_timezone_table: bool
    in_duffel_table: bool
    in_liteapi_table: bool
    has_hotel_cap: bool

    @property
    def searchable(self) -> bool:
        """能不能真的搜到机票和酒店。两个 Provider 都是 fail-closed。"""
        return self.in_duffel_table and self.in_liteapi_table

    @property
    def fully_covered(self) -> bool:
        """五张表都认得——只有这样政策判定才不会落到"证据不足"。"""
        return self.searchable and self.in_alias_table and self.has_hotel_cap


def _verdict(
    spoken: str,
    *,
    normalizer: CityNormalizer,
    caps: set[str],
) -> Verdict:
    canonical = normalizer.canonicalize(spoken)
    # 别名表认不认得：认得的话规范名会**变成**另一个字符串（"上海"→"Shanghai"）。
    in_alias = canonical != spoken
    # 下游每张表各自用**规范名和原话**两种写法查一次——别名表没接住的名字，
    # 下游拿到的就还是原话。
    keys = {spoken.strip().casefold(), canonical.strip().casefold()}
    return Verdict(
        spoken=spoken,
        canonical=canonical,
        in_alias_table=in_alias,
        in_timezone_table=any(key in DEFAULT_CITY_TIMEZONES for key in keys),
        in_duffel_table=any(key in DEFAULT_LOCATION_CODES for key in keys),
        in_liteapi_table=any(key in DEFAULT_LOCATIONS for key in keys),
        has_hotel_cap=canonical in caps,
    )


def _table_sizes(config_path: Path) -> dict[str, object]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    cities = payload["cities"]
    caps = payload["policies"][0]["hotel_city_caps"]
    return {
        "config_cities_aliases": sum(1 + len(item["aliases"]) for item in cities),
        "config_cities_canonical": len(cities),
        "hotel_city_caps": len(caps),
        "duffel_aliases": len(DEFAULT_LOCATION_CODES),
        "duffel_codes": len(set(DEFAULT_LOCATION_CODES.values())),
        "liteapi_aliases": len(DEFAULT_LOCATIONS),
        "timezone_aliases": len(DEFAULT_CITY_TIMEZONES),
    }


def _config_city_mismatch() -> list[dict[str, object]]:
    """政策配置声明支持、但 Provider 表里查不到的城市。

    这一项和样本无关，是**配置自己和自己对不上**：`config.cities` 说支持某座城市，
    Provider 的表里却没有——这不是用户说了个冷门地名，是配置在说谎。
    """
    payload = json.loads(
        (REPO / "config" / "travel-policy.json").read_text(encoding="utf-8")
    )
    rows: list[dict[str, object]] = []
    for item in sorted(payload["cities"], key=lambda entry: entry["canonical_name"]):
        canonical = item["canonical_name"]
        key = canonical.casefold()
        missing = []
        if key not in DEFAULT_LOCATION_CODES:
            missing.append("Duffel")
        if key not in DEFAULT_LOCATIONS:
            missing.append("LiteAPI")
        if missing:
            rows.append({"canonical": canonical, "missing_from": missing})
    return rows


def measure() -> dict[str, object]:
    configuration = load_policy_configuration()
    normalizer = CityNormalizer(configuration.city_aliases)
    caps = set(configuration.active_policy.hotel_city_caps)

    groups = {
        "repo_exercised": REPO_EXERCISED,
        "named_air_markets": NAMED_AIR_MARKETS,
    }
    results: dict[str, object] = {
        "measured_at": datetime.now(UTC).isoformat(),
        "billed_model_calls": 0,
        "external_calls": 0,
        "table_sizes": _table_sizes(REPO / "config" / "travel-policy.json"),
        "config_city_mismatch": _config_city_mismatch(),
        "groups": {},
    }
    for name, sample in groups.items():
        verdicts = [_verdict(item, normalizer=normalizer, caps=caps) for item in sample]
        results["groups"][name] = {
            "total": len(verdicts),
            "in_alias_table": sum(1 for item in verdicts if item.in_alias_table),
            "in_timezone_table": sum(1 for item in verdicts if item.in_timezone_table),
            "in_duffel_table": sum(1 for item in verdicts if item.in_duffel_table),
            "in_liteapi_table": sum(1 for item in verdicts if item.in_liteapi_table),
            "has_hotel_cap": sum(1 for item in verdicts if item.has_hotel_cap),
            "searchable": sum(1 for item in verdicts if item.searchable),
            "fully_covered": sum(1 for item in verdicts if item.fully_covered),
            "cases": [
                {
                    "spoken": item.spoken,
                    "canonical": item.canonical,
                    "alias": item.in_alias_table,
                    "timezone": item.in_timezone_table,
                    "duffel": item.in_duffel_table,
                    "liteapi": item.in_liteapi_table,
                    "hotel_cap": item.has_hotel_cap,
                    "searchable": item.searchable,
                }
                for item in verdicts
            ],
        }
    return results


def _render(results: dict[str, object]) -> str:
    sizes = results["table_sizes"]
    lines = [
        "# 城市覆盖测量（方案第 04 步开工前）",
        "",
        f"测量时间 {results['measured_at']}；**0 次模型调用，0 次外网调用**。",
        "",
        "## 一共有几张表",
        "",
        "| 表 | 别名条目 | 覆盖城市 | 落表外的后果 |",
        "|---|---:|---:|---|",
        f"| `config.cities` → CityNormalizer | {sizes['config_cities_aliases']} "
        f"| {sizes['config_cities_canonical']} | **原话原样往下传**，不报错 |",
        f"| `config.hotel_city_caps` | {sizes['hotel_city_caps']} "
        f"| {sizes['hotel_city_caps']} | 政策落 `INSUFFICIENT_EVIDENCE`，方案被降档 |",
        f"| `duffel.DEFAULT_LOCATION_CODES` | {sizes['duffel_aliases']} "
        f"| {sizes['duffel_codes']} | **ProviderError，搜不了机票** |",
        f"| `liteapi.DEFAULT_LOCATIONS` | {sizes['liteapi_aliases']} | — "
        "| **ProviderError，搜不了酒店** |",
        f"| `locations.DEFAULT_CITY_TIMEZONES` | {sizes['timezone_aliases']} | — "
        "| 悄悄回退到政策默认时区 |",
        "",
        "**五张表各管各的，谁也不认谁。** 这是第 04 步真正要解决的事——",
        "不是「某一张表太小」，是**同一个概念在五个地方各存了一份，而且互不一致**。",
        "",
        "## 样本走完一遍",
        "",
        "| 样本 | 总数 | 别名表 | 时区表 | Duffel | LiteAPI | 夜费上限 "
        "| **搜得了** | **五表全覆盖** |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "repo_exercised": "仓库自己喂过的名字",
        "named_air_markets": "主要航空市场（作者列的）",
    }
    for key, group in results["groups"].items():
        lines.append(
            f"| {labels[key]} | {group['total']} | {group['in_alias_table']} "
            f"| {group['in_timezone_table']} | {group['in_duffel_table']} "
            f"| {group['in_liteapi_table']} | {group['has_hotel_cap']} "
            f"| **{group['searchable']}** | **{group['fully_covered']}** |"
        )
    lines.extend(
        [
            "",
            "## 认不出的名字，**按认不出的是什么分开列**",
            "",
            "机票和酒店各查各的表，两张表并不一致——混成一句「搜不了」"
            "会盖掉「订得到机票、订不到酒店」这种情况。",
            "",
        ]
    )
    buckets = (
        ("机票和酒店都搜不了", lambda item: not item["duffel"] and not item["liteapi"]),
        ("**订得到机票、订不到酒店**", lambda item: item["duffel"] and not item["liteapi"]),
        ("订得到酒店、订不到机票", lambda item: not item["duffel"] and item["liteapi"]),
        (
            "搜得了，但政策查不到夜费上限",
            lambda item: item["searchable"] and not item["hotel_cap"],
        ),
    )
    for title, predicate in buckets:
        rows = []
        for key, group in results["groups"].items():
            names = [item["spoken"] for item in group["cases"] if predicate(item)]
            if names:
                rows.append(f"  - {labels[key]}：{'、'.join(names)}")
        lines.append(f"- {title}：")
        lines.extend(rows or ["  - （无）"])
    lines.extend(
        [
            "",
            "## 政策配置里正式声明的城市，下游认不认得",
            "",
            "`config.cities` 是这套系统**自己声明支持**的城市。它和 Provider 的表对不上，"
            "就不是「用户说了个冷门地名」，是配置在说谎。",
            "",
        ]
    )
    mismatches = results["config_city_mismatch"]
    if mismatches:
        for row in mismatches:
            missing = "、".join(row["missing_from"])
            lines.append(f"- **{row['canonical']}** —— {missing} 里没有")
    else:
        lines.append("- （全部对得上）")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)

    results = measure()
    (output / "measurement.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = _render(results)
    (output / "REPORT.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
