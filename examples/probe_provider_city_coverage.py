"""实测：候选城市在两家供应商那边**真的搜得到东西吗**。

方案第 04 步的另一半测量（HANDOFF §34.8）。城市登记表里 ``liteapi`` 留空表示
"这家供应商那边还没验证过这座城市"——**这个脚本就是去验证的那一步**。

**为什么必须实测、不能照着 ISO 国家码编一条：** 编进去只会把"搜不了"换成
"搜到了零家房，还说不清是没房、还是定位写错了"。前者 fail-closed，后者是静默错误。

两边各验一件事：

- **LiteAPI**：拿 ``{cityName, countryCode}`` 去搜，看有没有房。
- **Duffel**：拿我打算写进登记表的 IATA 城市码去搜一条航线，看这个码**认不认**。
  城市码不是随便记的——西安的机场是 XIY、城市码是 SIA，记混了会让整座城市搜不到票，
  而且报错只会说"没有航班"，看不出是码写错了。

**这个脚本会真的联网**：两家供应商各按候选城市查一次，只读。
不下单、不付款、不写任何东西。不调用任何语言模型。
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
)
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.providers.liteapi import LiteAPIHotelProvider

REPO = Path(__file__).resolve().parents[1]

#: 想加进登记表、但还没验证过的城市。中国大陆按客流排的主要航空市场为主——
#: 实测（`reports/evaluation-runs/city-coverage-*/`）说这些今天**一座都搜不了**。
CANDIDATES: tuple[tuple[str, str, str], ...] = (
    # (规范名, 国家码, IATA 城市码)
    ("Guangzhou", "CN", "CAN"),
    ("Shenzhen", "CN", "SZX"),
    ("Chengdu", "CN", "CTU"),
    ("Hangzhou", "CN", "HGH"),
    ("Nanjing", "CN", "NKG"),
    ("Xi'an", "CN", "SIA"),
    ("Chongqing", "CN", "CKG"),
    ("Wuhan", "CN", "WUH"),
    ("Qingdao", "CN", "TAO"),
    ("Xiamen", "CN", "XMN"),
    ("Tianjin", "CN", "TSN"),
    ("Changsha", "CN", "CSX"),
    ("Zhengzhou", "CN", "CGO"),
    ("Kunming", "CN", "KMG"),
    ("Dalian", "CN", "DLC"),
    ("Shenyang", "CN", "SHE"),
    ("Harbin", "CN", "HRB"),
    ("Sanya", "CN", "SYX"),
    # 已经在登记表里、但 LiteAPI 侧从没验证过的两座（见 KNOWN_GAPS）
    ("Dubai", "AE", "DXB"),
    ("Sydney", "AU", "SYD"),
)


#: 三种结果，**必须分开**：
#: - ``found``   有房，这座城市可以写进登记表
#: - ``empty``   供应商答了，但没有房——这是一个真的否定
#: - ``unknown`` 限流或超时，**什么都没证明**
#:
#: 第一版把 ``unknown`` 混进了否定，于是一次 HTTP 429 把 15 座城市报成了"没房"。
#: 这正是这一整步要消灭的那类错误：把"不知道"说成"没有"。
FOUND, EMPTY, UNKNOWN = "found", "empty", "unknown"


def _probe(
    provider: LiteAPIHotelProvider, city: str, check_in: date, check_out: date
) -> dict[str, object]:
    query = HotelSearchQuery(city=city, check_in=check_in, check_out=check_out)
    try:
        snapshot = provider.search_hotels(query)
    except RetryableProviderError as exc:
        return {"city": city, "verdict": UNKNOWN, "hotels": 0, "error": str(exc)[:200]}
    except ProviderError as exc:
        message = str(exc)
        # 429 / 超时这类是"这次没问到"，不是"这座城市没有房"。
        verdict = UNKNOWN if "429" in message or "timed out" in message else EMPTY
        return {"city": city, "verdict": verdict, "hotels": 0, "error": message[:200]}
    hotels = len(snapshot.items)
    return {
        "city": city,
        "verdict": FOUND if hotels else EMPTY,
        "hotels": hotels,
        "warnings": list(snapshot.provider_warnings)[:3],
    }


def _probe_flights(
    provider: DuffelProvider, code: str, depart_on: date
) -> dict[str, object]:
    """从北京飞过去，只为确认这个 IATA 码 Duffel 认不认。

    **搜到 0 条不算失败**——测试环境本来就不是每条航线都有货。
    失败的是"这个码根本不认"，那会是一条 ProviderError。
    """
    shanghai = ZoneInfo("Asia/Shanghai")
    origin = "PEK" if code != "PEK" else "SHA"
    query = TransportSearchQuery(
        origin=origin,
        destination=code,
        depart_after=datetime.combine(depart_on, time(0, 0), tzinfo=shanghai),
        arrive_before=datetime.combine(
            depart_on + timedelta(days=1), time(23, 59), tzinfo=shanghai
        ),
    )
    try:
        snapshot = provider.search_transport(query)
    except ProviderError as exc:
        message = str(exc)
        return {
            "code": code,
            "accepted": "IATA" not in message and "location" not in message.casefold(),
            "offers": 0,
            "error": message[:200],
        }
    return {"code": code, "accepted": True, "offers": len(snapshot.items)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=2.0,
        help="两次查询之间歇多久。沙箱会限流，问太快会把 429 当成'没房'",
    )
    parser.add_argument(
        "--confirm-live-provider-calls",
        action="store_true",
        required=True,
        help="确认这次会真的对 LiteAPI 沙箱发起只读查询（一座候选城市一次）",
    )
    args = parser.parse_args()
    output = args.output
    if not output.is_absolute():
        output = (REPO / output).resolve()
    output.mkdir(parents=True, exist_ok=False)

    # 把候选城市**临时**注入定位表——登记表本身一个字不改，
    # 验证通过的才由人写进 config/cities.json。
    locations = {
        name.casefold(): {"cityName": name, "countryCode": country}
        for name, country, _ in CANDIDATES
    }
    os.environ["LITEAPI_LOCATIONS_JSON"] = json.dumps(locations, ensure_ascii=False)
    provider = LiteAPIHotelProvider.from_environment()

    check_in = date.today() + timedelta(days=30)
    check_out = check_in + timedelta(days=2)
    # 沙箱会限流。慢一点问，比问快了拿一堆 429 当"没房"要好。
    hotel_rows = []
    for index, (name, _, _) in enumerate(CANDIDATES):
        if index:
            sleep(args.pause_seconds)
        hotel_rows.append(_probe(provider, name, check_in, check_out))

    flights = DuffelProvider.from_environment()
    flight_rows = []
    for index, (_, _, code) in enumerate(CANDIDATES):
        if index:
            sleep(args.pause_seconds)
        flight_rows.append(_probe_flights(flights, code, check_in))

    payload = {
        "probed_at": datetime.now(UTC).isoformat(),
        "billed_model_calls": 0,
        "liteapi_read_only_calls": len(hotel_rows),
        "duffel_read_only_calls": len(flight_rows),
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "hotels": hotel_rows,
        "flights": flight_rows,
    }
    (output / "probe.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    counts = {
        verdict: sum(1 for row in hotel_rows if row["verdict"] == verdict)
        for verdict in (FOUND, EMPTY, UNKNOWN)
    }
    print(
        f"只读查询：LiteAPI {len(hotel_rows)} 次、Duffel {len(flight_rows)} 次\n"
        f"  酒店：有房 {counts[FOUND]}、确实没房 {counts[EMPTY]}、"
        f"**没问到（限流等）{counts[UNKNOWN]}**\n"
        f"  IATA 码被认 {sum(1 for r in flight_rows if r['accepted'])}/{len(flight_rows)}"
    )
    marks = {FOUND: "✓", EMPTY: "✗", UNKNOWN: "?"}
    for (name, _, code), hotel, flight in zip(
        CANDIDATES, hotel_rows, flight_rows, strict=True
    ):
        flight_mark = "✓" if flight["accepted"] else "✗"
        detail = flight.get("error") or f"{flight['offers']} 条航班"
        print(
            f"  {name:<12} {code}  房 {marks[hotel['verdict']]} {hotel['hotels']:>2} 家   "
            f"码 {flight_mark} {detail}"
        )
    if counts[UNKNOWN]:
        print(
            "\n**有城市没问到，这份报告不完整**——隔一会儿重跑，"
            "别把「没问到」当成「没有」。"
        )
    print(f"\n报告 → {output}")


if __name__ == "__main__":
    main()
