"""第 03 步的验收闸门：三段行程 + 每站住宿，**走完整条编排链路**。

`tests/test_multi_city_planning.py` 证的是规划器算得出三段方案，但那是**直接调
规划器**。这个文件走的是 `TripWorkflowOrchestrator.create_task`：搜索、可行性、
政策、预算、报错文案全在里面。第 02 步之后规划器接口收得下 N 段，喂进去的却永远
是 2 段——本步拔掉的就是这个。

**住宿站**：一次过夜，在哪座城市、住哪几天。往返只有一站（目的地），多城有 N-1 站。

**不调模型、不访外网。** 语义层今天还只产出 1–2 段与一处住宿（方案第 05 步），
所以这里直接把 `journey` 与 `stays` 写死喂进去。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from corporate_travel_agent.demo import build_demo_system
from corporate_travel_agent.domain.enums import BookingScope, TaskState, TransportMode, TripLegRole
from corporate_travel_agent.domain.models import (
    HotelOffer,
    TransportOffer,
    TripLeg,
    TripRequestVersion,
    TripStay,
)
from corporate_travel_agent.domain.validation import validate_trip_request
from corporate_travel_agent.providers.mock import MockProvider

SH = ZoneInfo("Asia/Shanghai")
TOKYO = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=SH)

# 北京 → 上海（9/15 开会，住到 9/17）→ 东京（9/17 见客户，住到 9/19）→ 北京
# 两座住宿城市都在 config/travel-policy.json 的夜费上限表里——表外城市会落到
# INSUFFICIENT_EVIDENCE，那是第 04 步（城市别名表）的事，不该混进本步的闸门。
JOURNEY = (
    TripLeg(
        role=TripLegRole.OUTBOUND,
        origin="Beijing",
        destination="Shanghai",
        depart_after=datetime(2026, 9, 15, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 15, 22, 0, tzinfo=SH),
    ),
    TripLeg(
        role=TripLegRole.OUTBOUND,
        origin="Shanghai",
        destination="Tokyo",
        depart_after=datetime(2026, 9, 17, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 17, 23, 0, tzinfo=SH),
    ),
    TripLeg(
        role=TripLegRole.RETURN,
        origin="Tokyo",
        destination="Beijing",
        depart_after=datetime(2026, 9, 19, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 19, 23, 0, tzinfo=SH),
    ),
)

STAYS = (
    TripStay(city="Shanghai", check_in=date(2026, 9, 15), check_out=date(2026, 9, 17)),
    TripStay(city="Tokyo", check_in=date(2026, 9, 17), check_out=date(2026, 9, 19)),
)


def _transport(
    ref_id: str,
    leg: TripLeg,
    *,
    hour: int,
    hours: int = 3,
    price: str = "300",
    mode: TransportMode = TransportMode.FLIGHT,
) -> TransportOffer:
    day = leg.depart_after.date()
    depart = datetime(day.year, day.month, day.day, hour, 0, tzinfo=SH)
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="unused",
        provider="mock",
        mode=mode,
        origin=leg.origin,
        destination=leg.destination,
        depart_at=depart,
        arrive_at=depart.replace(hour=hour + hours),
        price=Decimal(price),
        seat_class="ECONOMY" if mode is TransportMode.FLIGHT else "SECOND_CLASS",
        is_direct=True,
        currency="USD",
    )


def _hotel(ref_id: str, stay: TripStay, *, nightly: str, commute: int = 20) -> HotelOffer:
    return HotelOffer(
        ref_id=ref_id,
        snapshot_id="unused",
        provider="mock",
        name=f"{stay.city} {ref_id}",
        city=stay.city,
        check_in=stay.check_in,
        check_out=stay.check_out,
        nightly_price=Decimal(nightly),
        commute_minutes=commute,
        currency="USD",
    )


def _inventory() -> tuple[list[TransportOffer], list[HotelOffer]]:
    transports = [
        _transport("BJ-SH-AM", JOURNEY[0], hour=8, price="300"),
        _transport("BJ-SH-PM", JOURNEY[0], hour=15, price="220"),
        _transport("SH-TY-AM", JOURNEY[1], hour=9, price="400"),
        _transport("TY-BJ-EVE", JOURNEY[2], hour=15, price="380"),
    ]
    hotels = [
        _hotel("SH-CHEAP", STAYS[0], nightly="180"),
        _hotel("SH-NICE", STAYS[0], nightly="240", commute=10),
        _hotel("TY-CHEAP", STAYS[1], nightly="150"),
    ]
    return transports, hotels


def _request(task_id: str, **overrides: object) -> TripRequestVersion:
    payload: dict[str, object] = {
        "task_id": task_id,
        "version": 1,
        "traveler_id": "E1001",
        # 扁平字段仍然只描述得了第一段与第一处住宿——这正是它们的极限。
        "origin": "Beijing",
        "destination": "Shanghai",
        "departure_after": JOURNEY[0].depart_after,
        "arrive_by": JOURNEY[0].arrive_before,
        "return_after": JOURNEY[2].depart_after,
        "return_before": JOURNEY[2].arrive_before,
        "hotel_check_in": STAYS[0].check_in,
        "hotel_check_out": STAYS[0].check_out,
        "booking_scope": BookingScope.ROUND_TRIP,
        "journey": JOURNEY,
        "stays": STAYS,
    }
    payload.update(overrides)
    return TripRequestVersion(**payload)  # type: ignore[arg-type]


def _system(
    transports: list[TransportOffer] | None = None,
    hotels: list[HotelOffer] | None = None,
    *,
    max_tool_calls: int = 12,
):
    stock_transports, stock_hotels = _inventory()
    provider = MockProvider(
        transports if transports is not None else stock_transports,
        hotels if hotels is not None else stock_hotels,
        clock=lambda: NOW,
    )
    workflow, _ = build_demo_system(
        clock=lambda: NOW, provider=provider, max_tool_calls=max_tool_calls
    )
    return workflow


class MultiCitySearchTests(unittest.TestCase):
    def test_a_three_leg_two_stay_trip_searches_each_one_exactly_once(self) -> None:
        """三段交通各搜一次，两站住宿各搜一次，外加一次**整票**。

        整票那一次是第 06 步（§37）加的：一次请求问完整条行程，供应商按 IATA 票价
        构造规则给一个覆盖全程的价——实测便宜 15%–76%。它是**额外**的一次，
        分段那几次一次都没少：整票和分段购买是两种真正不同的走法，一起摆出来由人取舍。
        """
        task = _system().create_task(_request("mc-search"))

        self.assertEqual(
            [item.tool_name for item in task.tool_calls],
            [
                "provider.search_transport.outbound",
                "provider.search_transport.inbound",
                "provider.search_transport.leg2",
                "provider.search_transport.journey",
                "provider.search_hotels",
                "provider.search_hotels.stay1",
            ],
        )

    def test_a_round_trip_does_not_pay_for_a_journey_fare_search_by_default(self) -> None:
        """**往返默认不问整票。**

        实测整票连往返都便宜 18%–23%，但对往返打开意味着每一趟差旅都多发一次
        供应商请求——那是项目所有者的取舍（HANDOFF §1.5 第 3 条），不是规划层该替
        他定的。默认 `journey_fare_min_legs=3`，要打开就设成 2。
        """
        transports, hotels = _inventory()
        request = _request(
            "mc-roundtrip",
            journey=(JOURNEY[0], JOURNEY[2]),
            stays=STAYS[:1],
        )
        task = _system(transports, hotels).create_task(request)

        self.assertNotIn(
            "provider.search_transport.journey",
            [item.tool_name for item in task.tool_calls],
        )

    def test_the_trip_plans_end_to_end_with_a_hotel_in_each_city(self) -> None:
        """三段行程真的出得来方案，而且**每站各有一家自己城市的酒店**。"""
        task = _system().create_task(_request("mc-plan"))

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        self.assertTrue(task.options, "三段行程没有出任何方案")
        for option in task.options:
            self.assertEqual([leg.origin for leg in option.legs], ["Beijing", "Shanghai", "Tokyo"])
            self.assertEqual([stay.city for stay in option.stays], ["Shanghai", "Tokyo"])
            # 总价把两处住宿都算进去了，不是只算第一处。
            self.assertEqual(
                option.total_cost,
                sum(leg.price for leg in option.legs)
                + sum(stay.total_price for stay in option.stays),
            )
            # 每一处住宿都要能被再校验到——否则第二家酒店等于没订。
            self.assertEqual(len(option.inventory_refs), 5)

    def test_the_old_single_slot_name_still_points_at_the_first_stay(self) -> None:
        """`hotel` 降级成了属性——前端与评测读到的东西不变。"""
        option = _system().create_task(_request("mc-hotel-alias")).options[0]

        self.assertIs(option.hotel, option.stays[0])
        # 第二处住宿没有旧名字可用，只能从 stays 读——这正是为什么要有 stays。
        self.assertEqual(option.stays[1].city, "Tokyo")

    def test_the_second_stay_shows_up_in_the_explanation_facts(self) -> None:
        """第一处沿用 hotel=，第二处起才是 stay1=。"""
        option = _system().create_task(_request("mc-facts")).options[0]
        facts = dict(item.split("=", 1) for item in option.explanation_facts)

        self.assertEqual(facts["hotel"], option.stays[0].ref_id)
        self.assertEqual(facts["stay1"], option.stays[1].ref_id)
        self.assertIn("stay1_commute_minutes", facts)

    def test_the_budget_counts_every_leg_and_every_stay(self) -> None:
        """预算按"三段 + 两站 = 五次"算。

        此前住宿无论几站都只算一次，多城行程会在预算只够四次时开搜，
        搜到第二站才发现调用用光——已经花掉的四次全白花。
        """
        task = _system(max_tool_calls=4).create_task(_request("mc-budget"))

        self.assertEqual(task.state, TaskState.TOOL_BUDGET_EXHAUSTED)
        self.assertEqual(task.tool_calls_used, 0, "预算不够就不该开搜")

    def test_a_middle_leg_with_no_inventory_says_which_leg(self) -> None:
        """中间段搜不到，话里要说得出是哪一程。

        往返里"没有返程"已经够清楚了，但三段行程的第 1 段是**中途那一段**、
        不是返程，光说"leg 1"谁也不知道指哪一程——所以带上航线。
        """
        transports, hotels = _inventory()
        without_middle = [item for item in transports if item.origin != "Shanghai"]
        task = _system(without_middle, hotels).create_task(_request("mc-gap"))

        self.assertEqual(task.state, TaskState.NO_FEASIBLE_OPTION)
        self.assertIn(
            "no leg 1 (Shanghai → Tokyo) inventory matched the requested route and time window",
            task.metadata["no_feasible_reasons"],
        )

    def test_a_stop_with_no_hotel_says_which_city(self) -> None:
        """第二站没酒店，话里要说得出是哪座城市。"""
        transports, hotels = _inventory()
        without_tokyo = [item for item in hotels if item.city != "Tokyo"]
        task = _system(transports, without_tokyo).create_task(_request("mc-nohotel"))

        self.assertEqual(task.state, TaskState.NO_FEASIBLE_OPTION)
        self.assertIn(
            "no hotel inventory matched Tokyo for the requested dates",
            task.metadata["no_feasible_reasons"],
        )

    def test_a_one_stay_trip_still_searches_hotels_under_the_old_name(self) -> None:
        """只有一站时，工具名与话术一个字都没变——旧任务读到的东西不变。"""
        transports, hotels = _inventory()
        request = _request(
            "mc-single",
            journey=JOURNEY[:1],
            return_after=None,
            return_before=None,
            booking_scope=BookingScope.OUTBOUND_ONLY,
            stays=STAYS[:1],
        )
        task = _system(transports, hotels).create_task(request)

        self.assertEqual(
            [item.tool_name for item in task.tool_calls],
            ["provider.search_transport.outbound", "provider.search_hotels"],
        )
        option = task.options[0]
        self.assertEqual([stay.city for stay in option.stays], ["Shanghai"])


class JourneyFareTests(unittest.TestCase):
    """整票：一次请求问完整条行程，供应商给一个覆盖全程的价。

    **整票和分段购买是两种真正不同的走法**（§30.2），所以两边的方案一起摆出来、
    由人取舍——不是二选一。整票便宜（实测 15%–76%），分段可以各段单独退改。
    """

    def _system_with_discount(self, discount: str):
        stock_transports, stock_hotels = _inventory()
        provider = MockProvider(stock_transports, stock_hotels, clock=lambda: NOW)
        provider.journey_fare_discount = Decimal(discount)
        workflow, _ = build_demo_system(
            clock=lambda: NOW, provider=provider, max_tool_calls=12
        )
        return workflow

    def test_the_cheaper_journey_fare_is_put_on_the_table(self) -> None:
        """整票便宜就该被摆出来，而且它的几段带着同一个 fare_ref。"""
        task = self._system_with_discount("0.7").create_task(_request("mc-fare"))

        fares = [
            option
            for option in task.options
            if any(leg.fare_ref for leg in option.legs)
        ]
        self.assertTrue(fares, "整票便宜 30% 却一条都没摆出来")
        option = fares[0]
        refs = {leg.fare_ref for leg in option.legs}
        self.assertEqual(len(refs), 1, "一张整票的几段必须共用一个 fare_ref")

    def test_a_fare_is_priced_as_one_ticket_not_three(self) -> None:
        """整票只有一个价。

        `sum(leg.price)` 仍然是真实总价，但**单看某一段的 price 没有意义**——
        整票的价只记在第一段上。要展示价格请读 `option.fares`。
        """
        task = self._system_with_discount("0.7").create_task(_request("mc-fare-price"))
        option = next(
            item for item in task.options if any(leg.fare_ref for leg in item.legs)
        )

        # 三段交通只对应**一张票**，不是三张。
        transport_fares = [
            (ref, total) for ref, total in option.fares if ref is not None
        ]
        self.assertEqual(len(transport_fares), 1)
        self.assertEqual(
            transport_fares[0][1], sum(leg.price for leg in option.legs)
        )

    def test_both_ways_of_buying_stay_on_the_table(self) -> None:
        """分段购买的方案不许因为整票更便宜就消失。

        便宜不是唯一的取舍：整票的几段绑在一起，分段可以各段单独退改。
        **取舍交回给人**，不由规划器代劳。
        """
        task = self._system_with_discount("0.7").create_task(_request("mc-both"))

        has_fare = any(any(leg.fare_ref for leg in o.legs) for o in task.options)
        has_split = any(all(not leg.fare_ref for leg in o.legs) for o in task.options)
        self.assertTrue(has_fare, "整票没被摆出来")
        self.assertTrue(has_split, "分段购买被整票挤掉了")

    def test_a_provider_without_multi_city_still_plans(self) -> None:
        """供应商做不了整票时退回分段购买——**少省一笔钱，不是这趟走不了**。"""

        class NoJourneyProvider:
            """去掉 search_multi_city 的替身；其余行为原样委托。"""

            def __init__(self, inner: MockProvider) -> None:
                self._inner = inner
                self.name = inner.name

            def __getattr__(self, item: str) -> object:
                if item == "search_multi_city":
                    raise AttributeError(item)
                return getattr(self._inner, item)

        stock_transports, stock_hotels = _inventory()
        provider = NoJourneyProvider(
            MockProvider(stock_transports, stock_hotels, clock=lambda: NOW)
        )
        workflow, _ = build_demo_system(clock=lambda: NOW, provider=provider)
        task = workflow.create_task(_request("mc-no-journey"))

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER, task.failure)
        self.assertTrue(task.options)
        self.assertNotIn(
            "provider.search_transport.journey",
            [item.tool_name for item in task.tool_calls],
        )


class StayValidationTests(unittest.TestCase):
    """住宿站是第二个"结构化字段盖过扁平字段"的地方，守门规矩和航段一样。

    没有守门人的话，一份说不通的住宿列表会被原样送去 Provider 查询——
    坏的输入不会在这里停下，会变成一次真花钱的搜索。
    """

    def _conflicts(self, **overrides: object) -> tuple[str, ...]:
        return validate_trip_request(_request("mc-validate", **overrides)).conflicts

    def test_a_good_stay_list_passes(self) -> None:
        self.assertEqual(self._conflicts(), ())

    def test_a_stay_cannot_check_out_before_it_checks_in(self) -> None:
        same_day = date(2026, 9, 17)
        bad = (TripStay(city="Shanghai", check_in=same_day, check_out=same_day),)
        self.assertIn(
            "stay 0 must check out after it checks in",
            self._conflicts(
                stays=bad, hotel_check_in=same_day, hotel_check_out=same_day
            ),
        )

    def test_stays_cannot_overlap_each_other(self) -> None:
        """在上一站还没退房时就在下一座城市入住，是一份说不通的行程。"""
        overlapping = (
            STAYS[0],
            TripStay(city="Tokyo", check_in=date(2026, 9, 16), check_out=date(2026, 9, 19)),
        )
        self.assertIn(
            "stay 1 checks in before stay 0 checks out",
            self._conflicts(stays=overlapping),
        )

    def test_a_journey_cannot_have_more_stops_than_it_has_legs(self) -> None:
        """三段行程最多过夜两次——最后一段落地就到家了。"""
        too_many = (
            *STAYS,
            TripStay(
                city="Beijing",
                check_in=date(2026, 9, 19),
                check_out=date(2026, 9, 20),
            ),
        )
        self.assertIn(
            "stays has 3 stops; a 3-leg journey can stay over at most 2 times",
            self._conflicts(stays=too_many),
        )

    def test_the_two_views_must_say_the_same_thing(self) -> None:
        """第一站就是扁平字段说的那一次住宿，两边不许讲不同的话。"""
        self.assertIn(
            "stays[0] check-in disagrees with hotel_check_in",
            self._conflicts(hotel_check_in=date(2026, 9, 14)),
        )


if __name__ == "__main__":
    unittest.main()
