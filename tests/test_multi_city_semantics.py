"""第 05 步的验收闸门：语义层读出三段行程，编译成 `journey` 与 `stays`。

**这是六步里唯一要花钱的一步，所以先量了再做。** 实测（真实模型 deepseek-v4-pro、
prompt v13，`reports/evaluation-runs/multicity-extraction-*/`）：六条多城原话各跑三次，
**段数、每段起讫、走的顺序、过夜城市 21/21 全对**——包括一条故意乱序叙述的
（先说西安后说南京，模型仍按走的顺序排）。所以剩下的活是宿主接线，不是教模型读。

这个文件只用**脚本化模型**，一次都不调真模型：真模型能不能读是上面那次测量回答的，
这里回答的是"读出来之后宿主接不接得住"。两件事分开测，坏了才知道是哪一边。

**航段**：一次起讫。**住宿站**：一次过夜，在哪座城市、住哪几天。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from corporate_travel_agent.agent.search_command import compile_search_command
from corporate_travel_agent.agent.semantic_intent import (
    EvidenceRef,
    IntentDecision,
    IntentDecisionStatus,
    SemanticIntent,
    SemanticLeg,
    SemanticStay,
)
from corporate_travel_agent.domain.enums import (
    BookingScope,
    LodgingRequirement,
    TransportMode,
)
from corporate_travel_agent.domain.models import TransportOffer
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration

SH = ZoneInfo("Asia/Shanghai")
CREATED = datetime(2026, 8, 19, 15, 0, tzinfo=SH)

# 北京 → 上海（9/15 开会）→ 杭州（9/18 见客户）→ 北京（9/19 回）
THREE_LEGS = (
    SemanticLeg(
        origin="北京",
        destination="上海",
        depart_after=datetime(2026, 9, 15, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 15, 22, 0, tzinfo=SH),
    ),
    SemanticLeg(
        origin="上海",
        destination="杭州",
        depart_after=datetime(2026, 9, 18, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 18, 22, 0, tzinfo=SH),
    ),
    SemanticLeg(
        origin="杭州",
        destination="北京",
        depart_after=datetime(2026, 9, 19, 6, 0, tzinfo=SH),
        arrive_before=datetime(2026, 9, 19, 23, 0, tzinfo=SH),
    ),
)

TWO_STAYS = (
    SemanticStay(city="上海", check_in=date(2026, 9, 15), check_out=date(2026, 9, 18)),
    SemanticStay(city="杭州", check_in=date(2026, 9, 18), check_out=date(2026, 9, 19)),
)


def _offer(ref_id: str, leg: SemanticLeg, *, hour: int, price: str) -> TransportOffer:
    day = leg.depart_after.date()  # type: ignore[union-attr]
    depart = datetime(day.year, day.month, day.day, hour, 0, tzinfo=SH)
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="unused",
        provider="mock",
        mode=TransportMode.FLIGHT,
        origin={"北京": "Beijing", "上海": "Shanghai", "杭州": "Hangzhou"}[leg.origin],
        destination={"北京": "Beijing", "上海": "Shanghai", "杭州": "Hangzhou"}[
            leg.destination
        ],
        depart_at=depart,
        arrive_at=depart.replace(hour=hour + 2),
        price=Decimal(price),
        seat_class="ECONOMY",
        is_direct=True,
        currency="USD",
    )


def _provider() -> MockProvider:
    return MockProvider(
        [
            _offer("BJ-SH", THREE_LEGS[0], hour=8, price="300"),
            _offer("SH-HZ", THREE_LEGS[1], hour=9, price="120"),
            _offer("HZ-BJ", THREE_LEGS[2], hour=15, price="330"),
        ],
        [],
        clock=lambda: CREATED,
    )


def _intent(**overrides: object) -> SemanticIntent:
    values: dict[str, object] = {
        "summary": "北京去上海开会，再去杭州见客户，最后回北京",
        "origin_candidates": ["北京"],
        "destination_candidates": ["上海"],
        "departure_after": THREE_LEGS[0].depart_after,
        "arrive_by": THREE_LEGS[0].arrive_before,
        "return_after": THREE_LEGS[-1].depart_after,
        "return_before": THREE_LEGS[-1].arrive_before,
        "booking_scope": BookingScope.ROUND_TRIP,
        "lodging_requirement": LodgingRequirement.NOT_REQUIRED,
        "hotel_check_in": None,
        "hotel_check_out": None,
        "client_location": None,
        "hard_constraints": [],
        "soft_preferences": [],
        "alternatives": [],
        "conditions": [],
        "uncertainties": [],
        "legs": list(THREE_LEGS),
    }
    values.update(overrides)
    return SemanticIntent(**values)


def _compile(intent: SemanticIntent):
    decision = IntentDecision(
        status=IntentDecisionStatus.READY,
        intent=intent,
        clarification_question=None,
        conflicts=[],
        unsupported_reasons=[],
        assumptions=[],
        evidence=[
            EvidenceRef(turn_index=0, field="origin_candidates", quote="北京"),
            EvidenceRef(turn_index=0, field="destination_candidates", quote="上海"),
            EvidenceRef(turn_index=0, field="departure_after", quote="9月15号"),
            EvidenceRef(turn_index=0, field="arrive_by", quote="9月15号"),
        ],
        confidence=0.95,
        manipulation_detected=False,
    )
    return compile_search_command(
        decision,
        task_id="mc-semantic",
        traveler_id="E1001",
        version=1,
        city_normalizer=CityNormalizer(load_policy_configuration().city_aliases),
        created_at=CREATED,
    )


class MultiCitySemanticsTests(unittest.TestCase):
    def test_three_legs_compile_into_an_ordered_journey(self) -> None:
        """本步的闸门：模型给三段，编译出来就是三段，顺序与起讫一字不改。"""
        compiled = _compile(_intent())

        self.assertTrue(compiled.ready, compiled.conflicts or compiled.missing)
        assert compiled.command is not None
        journey = compiled.command.request.journey
        self.assertEqual(len(journey), 3)
        self.assertEqual(
            [(leg.origin, leg.destination) for leg in journey],
            [("Beijing", "Shanghai"), ("Shanghai", "Hangzhou"), ("Hangzhou", "Beijing")],
        )

    def test_the_city_names_go_through_the_alias_table(self) -> None:
        """模型给中文，下游拿到规范名——**杭州能译出来是第 04 步的成果**。

        第 04 步之前杭州不在别名表里，"杭州"会原样传到 Provider 查询。
        """
        compiled = _compile(_intent())
        assert compiled.command is not None

        self.assertEqual(compiled.command.request.journey[1].destination, "Hangzhou")

    def test_two_stays_compile_into_one_stop_each(self) -> None:
        """两处住宿各归各的城市，不是把第一处放大到全程。"""
        compiled = _compile(
            _intent(
                lodging_requirement=LodgingRequirement.REQUIRED,
                hotel_check_in=date(2026, 9, 15),
                hotel_check_out=date(2026, 9, 18),
                stays=list(TWO_STAYS),
            )
        )

        self.assertTrue(compiled.ready, compiled.conflicts or compiled.missing)
        assert compiled.command is not None
        stays = compiled.command.request.stays
        self.assertEqual([stay.city for stay in stays], ["Shanghai", "Hangzhou"])
        self.assertEqual(stays[0].check_in, date(2026, 9, 15))
        self.assertEqual(stays[1].check_out, date(2026, 9, 19))

    def test_a_plain_round_trip_never_goes_through_the_legs_array(self) -> None:
        """一两段仍然走扁平字段。

        **加了 legs 之后最容易出的退步**：把普通往返也塞进 legs，两个视图从此
        各说各话。实测里 MC-05 就是盯这条的对照组，真模型 3/3 留空。
        """
        compiled = _compile(_intent(legs=[]))

        self.assertTrue(compiled.ready, compiled.conflicts or compiled.missing)
        assert compiled.command is not None
        journey = compiled.command.request.journey
        self.assertEqual(
            [(leg.origin, leg.destination) for leg in journey],
            [("Beijing", "Shanghai"), ("Shanghai", "Beijing")],
        )

    def test_a_leg_with_a_missing_window_falls_back_instead_of_inventing_one(self) -> None:
        """**没说的不许编。**

        实测里模型确实会这样：它读对了三段，但旅行者只说了开会日、没说出发日，
        于是它把时间窗留空并追问（MC-03，3/3 都问对了）。宿主这时不能拿另一段的
        时间凑一个出来——退回扁平字段，缺的照旧走追问。
        """
        incomplete = (
            THREE_LEGS[0],
            SemanticLeg(
                origin="上海",
                destination="杭州",
                depart_after=None,
                arrive_before=None,
            ),
            THREE_LEGS[2],
        )
        compiled = _compile(_intent(legs=list(incomplete)))
        assert compiled.command is not None

        journey = compiled.command.request.journey
        self.assertEqual(len(journey), 2, "缺时间窗的三段不该被硬编成三段")
        self.assertNotIn("Hangzhou", [leg.destination for leg in journey])

    def test_two_stays_with_a_missing_date_are_dropped_whole(self) -> None:
        """住宿日期同理：缺一个就整条不用，不拿另一站的日期凑。"""
        broken = (
            TWO_STAYS[0],
            SemanticStay(city="杭州", check_in=date(2026, 9, 18), check_out=None),
        )
        compiled = _compile(
            _intent(
                lodging_requirement=LodgingRequirement.REQUIRED,
                hotel_check_in=date(2026, 9, 15),
                hotel_check_out=date(2026, 9, 18),
                stays=list(broken),
            )
        )
        assert compiled.command is not None

        # 退回去的是"多站"，不是"住宿"：那一对日期推成在第一段目的地住一次——
        # 航段和住宿站是唯一真源，以前 `lodging_stays()` 推出来的那一条现在就在 stays 里。
        self.assertEqual(len(compiled.command.request.stays), 1)
        self.assertEqual(
            compiled.command.request.stays[0].city, compiled.command.request.destination
        )
        self.assertEqual(compiled.command.request.hotel_check_in, date(2026, 9, 15))


    def test_stays_are_dropped_when_the_traveller_never_asked_for_a_hotel(self) -> None:
        """**过夜是事实，订不订房是旅行者的决定。**

        真跑抓到的（`semantic-multiturn-mt03-*`）：多城行程模型会主动填 stays——
        行程确实要在上海、杭州过夜——但旅行者一个字没提订房。把它原样传下去就会
        拼出一个自相矛盾的请求（有住宿站、没有住宿日期），第 03 步的校验正确地
        拒绝了它，用户看到的却是一句莫名其妙的追问。宿主该在编译时就按住。
        """
        compiled = _compile(
            _intent(
                lodging_requirement=LodgingRequirement.UNSPECIFIED,
                stays=list(TWO_STAYS),
            )
        )

        self.assertTrue(compiled.ready, compiled.conflicts or compiled.missing)
        assert compiled.command is not None
        self.assertEqual(compiled.command.request.stays, ())
        # 三段航段不受影响——被按住的是住宿，不是行程。
        self.assertEqual(len(compiled.command.request.journey), 3)


if __name__ == "__main__":
    unittest.main()
