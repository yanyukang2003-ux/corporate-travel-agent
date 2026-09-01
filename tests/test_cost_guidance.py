"""代价说明的测试：贵多少、超标多少、该谁批、换一条能省多少。

第一组端到端：真的跑演示工作流，拿真实的政策证据算。它盯的是接线——
夜费超标的数字有没有真的从政策引擎传出来、审批人写的是不是真正会收到审批的那个人。

第二组是边界：混币种、没有合规基准、更便宜但更麻烦的方案、没有航段的方案。
每一条都有一个容易写错的默认答案。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TransportMode
from corporate_travel_agent.domain.models import (
    FeasibilityResult,
    PolicyDecision,
    RuleEvidence,
    TransportOffer,
    TravelOptionVersion,
)
from corporate_travel_agent.planning.cost_guidance import build_cost_guidance

_BASE_DEPARTURE = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)


def _leg(ref_id: str, *, depart_offset_minutes: int = 0, price: str = "0") -> TransportOffer:
    depart = _BASE_DEPARTURE + timedelta(minutes=depart_offset_minutes)
    return TransportOffer(
        ref_id=ref_id,
        snapshot_id="snap-1",
        provider="mock",
        mode=TransportMode.FLIGHT,
        origin="Beijing",
        destination="Shanghai",
        depart_at=depart,
        arrive_at=depart + timedelta(minutes=120),
        price=Decimal(price),
        seat_class="ECONOMY",
    )


def _option(
    option_id: str,
    *,
    cost: str,
    outcome: PolicyOutcome = PolicyOutcome.COMPLIANT,
    evidence: tuple[RuleEvidence, ...] = (),
    depart_offset_minutes: int = 0,
    duration_minutes: int = 120,
    currency: str = "USD",
    with_legs: bool = True,
) -> TravelOptionVersion:
    return TravelOptionVersion(
        option_id=option_id,
        version=1,
        trip_request_version=1,
        inventory_snapshot_ids=("snap-1",),
        legs=(
            (_leg(f"{option_id}-leg", depart_offset_minutes=depart_offset_minutes),)
            if with_legs
            else ()
        ),
        stays=(),
        total_cost=Decimal(cost),
        total_duration_minutes=duration_minutes,
        feasibility=FeasibilityResult(feasible=True, reasons=()),
        policy_decision=PolicyDecision(outcome=outcome, evidence=evidence),
        preference_penalty=Decimal("0"),
        score=Decimal("0"),
        explanation_facts=(),
        currency=currency,
    )


def _numeric_evidence(actual: str, threshold: str) -> RuleEvidence:
    return RuleEvidence(
        rule_id="hotel.city.nightly_cap",
        actual=actual,
        threshold=threshold,
        policy_version="v1",
        outcome=PolicyOutcome.REQUIRES_APPROVAL,
        message="",
        exception_allowed=True,
        actual_amount=Decimal(actual),
        threshold_amount=Decimal(threshold),
        amount_currency="USD",
    )


class CostGuidanceEndToEndTests(unittest.TestCase):
    """跑真链路，用真实的政策证据。"""

    def setUp(self) -> None:
        self.workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        self.task = self.workflow.create_task(make_demo_request(task_id="guidance"))
        self.guidance = {
            item.option_id: item
            for item in build_cost_guidance(
                self.task.options, approver_id=self.task.employee.manager_id
            )
        }

    def test_hotel_overspend_comes_out_as_a_number_not_a_verdict(self) -> None:
        """"超出差标 120/晚"——这个数要从政策证据里算出来，不是 parse 展示文本。"""
        over_cap = next(
            item
            for item in self.task.options
            if item.hotel is not None and item.hotel.ref_id == "HT-NEAR"
        )

        overages = self.guidance[over_cap.option_id].policy_overages

        self.assertEqual(len(overages), 1)
        self.assertEqual(overages[0].rule_id, "hotel.city.nightly_cap")
        self.assertEqual(overages[0].amount, Decimal("120"))
        self.assertEqual(overages[0].unit, "per_night")
        self.assertEqual(overages[0].currency, "USD")
        self.assertTrue(overages[0].exception_allowed)

    def test_the_named_approver_is_the_one_who_will_actually_get_it(self) -> None:
        """卡片上写的"该谁批"和真正落到谁头上，必须是同一个人。"""
        needs_approval = next(
            item
            for item in self.task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )

        named = self.guidance[needs_approval.option_id].approver_id
        self.workflow.select_option(
            self.task.task_id, needs_approval.option_id, business_reason="客户会议"
        )
        actual = self.workflow.tasks.get(self.task.task_id).approval.approver_id

        self.assertEqual(named, actual)

    def test_compliant_options_are_not_told_who_would_approve_them(self) -> None:
        compliant = next(
            item
            for item in self.task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )

        self.assertIsNone(self.guidance[compliant.option_id].approver_id)

    def test_a_cheaper_compliant_option_shows_up_with_its_price_tag(self) -> None:
        """省钱和代价必须一起出现，否则是诱导不是引导。"""
        over_cap = next(
            item
            for item in self.task.options
            if item.hotel is not None and item.hotel.ref_id == "HT-NEAR"
        )

        tradeoffs = self.guidance[over_cap.option_id].tradeoffs

        self.assertTrue(tradeoffs)
        self.assertGreater(tradeoffs[0].saves, 0)
        # 代价字段和省钱字段在同一个对象上，读的人拆不开。
        self.assertIsInstance(tradeoffs[0].departure_delta_minutes, int)
        self.assertIsInstance(tradeoffs[0].duration_delta_minutes, int)

    def test_guidance_costs_no_extra_tool_calls(self) -> None:
        """这一层只读已经摆出来的方案，不许多花一次供应商或模型调用。"""
        before = self.workflow.tasks.get(self.task.task_id).tool_calls_used

        build_cost_guidance(self.task.options, approver_id="M2001")

        self.assertEqual(
            self.workflow.tasks.get(self.task.task_id).tool_calls_used, before
        )


class CostGuidanceBoundaryTests(unittest.TestCase):
    def test_premium_is_measured_against_the_cheapest_fully_compliant_option(
        self,
    ) -> None:
        options = (
            _option("cheap-approval", cost="800", outcome=PolicyOutcome.REQUIRES_APPROVAL),
            _option("compliant", cost="1000"),
            _option("pricey", cost="1500", outcome=PolicyOutcome.REQUIRES_APPROVAL),
        )

        guidance = {item.option_id: item for item in build_cost_guidance(options, approver_id="M1")}

        # 基准是 1000 那条合规方案，不是 800 那条更便宜但需审批的——
        # 拿超标方案当基准会把"你已经超标了"藏起来。
        self.assertEqual(guidance["pricey"].cheapest_compliant_option_id, "compliant")
        self.assertEqual(guidance["pricey"].premium_over_cheapest_compliant, Decimal("500"))
        # 比基准还便宜的方案溢价是 0，不是负数。
        self.assertEqual(
            guidance["cheap-approval"].premium_over_cheapest_compliant, Decimal("0")
        )

    def test_no_compliant_option_means_no_premium_rather_than_zero(self) -> None:
        """一条合规方案都没有时，"贵多少"没有基准，答案是没有，不是 0。"""
        options = (
            _option("a", cost="800", outcome=PolicyOutcome.REQUIRES_APPROVAL),
            _option("b", cost="900", outcome=PolicyOutcome.REQUIRES_APPROVAL),
        )

        guidance = build_cost_guidance(options, approver_id="M1")

        self.assertIsNone(guidance[0].premium_over_cheapest_compliant)
        self.assertIsNone(guidance[0].cheapest_compliant_option_id)

    def test_mixed_currencies_produce_no_cross_option_arithmetic(self) -> None:
        """省的是 800 什么？说不出来就一句建议都不给。"""
        options = (
            _option("usd", cost="1000"),
            _option("cny", cost="900", currency="CNY"),
        )

        guidance = build_cost_guidance(options, approver_id="M1")

        for item in guidance:
            with self.subTest(option=item.option_id):
                self.assertEqual(item.tradeoffs, ())
                self.assertIsNone(item.premium_over_cheapest_compliant)

    def test_mixed_currencies_still_report_each_option_own_overspend(self) -> None:
        """方案自己和政策比不需要跨方案相加，所以币种不一致不影响它。"""
        options = (
            _option(
                "usd",
                cost="1000",
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                evidence=(_numeric_evidence("320", "200"),),
            ),
            _option("cny", cost="900", currency="CNY"),
        )

        guidance = build_cost_guidance(options, approver_id="M1")

        self.assertEqual(guidance[0].policy_overages[0].amount, Decimal("120"))

    def test_a_cheaper_but_more_troublesome_option_is_not_suggested(self) -> None:
        """更便宜但要多走一轮审批，那不是省，是麻烦。"""
        options = (
            _option("compliant-pricey", cost="1200"),
            _option("cheap-approval", cost="800", outcome=PolicyOutcome.REQUIRES_APPROVAL),
        )

        guidance = {item.option_id: item for item in build_cost_guidance(options, approver_id="M1")}

        self.assertEqual(guidance["compliant-pricey"].tradeoffs, ())
        # 反过来是可以建议的：从需审批换到合规，既省钱又少一道手续。
        self.assertEqual(
            [item.option_id for item in guidance["cheap-approval"].tradeoffs], []
        )

    def test_switching_from_approval_to_a_cheaper_compliant_option_is_suggested(
        self,
    ) -> None:
        options = (
            _option("approval-pricey", cost="1500", outcome=PolicyOutcome.REQUIRES_APPROVAL),
            _option("compliant-cheap", cost="900"),
        )

        guidance = {item.option_id: item for item in build_cost_guidance(options, approver_id="M1")}

        tradeoffs = guidance["approval-pricey"].tradeoffs
        self.assertEqual([item.option_id for item in tradeoffs], ["compliant-cheap"])
        self.assertEqual(tradeoffs[0].saves, Decimal("600"))

    def test_departure_and_duration_deltas_carry_a_sign(self) -> None:
        """"晚走两小时省 800"和"早走两小时省 800"是两句不同的话。"""
        options = (
            _option("now", cost="1500", depart_offset_minutes=0, duration_minutes=120),
            _option(
                "later", cost="700", depart_offset_minutes=120, duration_minutes=200
            ),
        )

        guidance = build_cost_guidance(options, approver_id="M1")[0]

        self.assertEqual(guidance.tradeoffs[0].saves, Decimal("800"))
        self.assertEqual(guidance.tradeoffs[0].departure_delta_minutes, 120)
        self.assertEqual(guidance.tradeoffs[0].duration_delta_minutes, 80)

    def test_tradeoffs_are_ranked_by_savings_and_capped(self) -> None:
        options = (
            _option("base", cost="2000"),
            _option("saves-100", cost="1900"),
            _option("saves-500", cost="1500"),
            _option("saves-300", cost="1700"),
        )

        guidance = build_cost_guidance(options, approver_id="M1", max_tradeoffs=2)[0]

        self.assertEqual(
            [item.option_id for item in guidance.tradeoffs], ["saves-500", "saves-300"]
        )

    def test_an_option_without_legs_reports_no_departure_difference(self) -> None:
        """说不出差别就不要编一个差别出来。"""
        options = (
            _option("no-legs", cost="1500", with_legs=False),
            _option("cheaper", cost="900"),
        )

        guidance = build_cost_guidance(options, approver_id="M1")[0]

        self.assertEqual(guidance.tradeoffs[0].saves, Decimal("600"))
        self.assertEqual(guidance.tradeoffs[0].departure_delta_minutes, 0)

    def test_a_rule_without_a_numeric_threshold_reports_no_overage(self) -> None:
        """舱位超标没有"超出多少钱"这个数——它只在政策证据里，不在这里编一个。"""
        seat_class = RuleEvidence(
            rule_id="transport.flight.seat_class",
            actual="BUSINESS",
            threshold="ECONOMY",
            policy_version="v1",
            outcome=PolicyOutcome.REQUIRES_APPROVAL,
            message="",
            exception_allowed=True,
        )
        options = (
            _option(
                "business",
                cost="1500",
                outcome=PolicyOutcome.REQUIRES_APPROVAL,
                evidence=(seat_class,),
            ),
        )

        guidance = build_cost_guidance(options, approver_id="M1")[0]

        self.assertEqual(guidance.policy_overages, ())
        # 但"该谁批"照样说得出来。
        self.assertEqual(guidance.approver_id, "M1")

    def test_a_rule_within_its_cap_is_not_reported_as_an_overspend(self) -> None:
        within = RuleEvidence(
            rule_id="hotel.city.nightly_cap",
            actual="180",
            threshold="200",
            policy_version="v1",
            outcome=PolicyOutcome.COMPLIANT,
            message="",
            exception_allowed=False,
            actual_amount=Decimal("180"),
            threshold_amount=Decimal("200"),
            amount_currency="USD",
        )
        self.assertEqual(within.overage_amount, Decimal("0"))

        guidance = build_cost_guidance(
            (_option("ok", cost="900", evidence=(within,)),), approver_id="M1"
        )[0]

        self.assertEqual(guidance.policy_overages, ())

    def test_unknown_approver_is_left_blank_rather_than_invented(self) -> None:
        options = (_option("a", cost="900", outcome=PolicyOutcome.REQUIRES_APPROVAL),)

        guidance = build_cost_guidance(options, approver_id=None)[0]

        self.assertIsNone(guidance.approver_id)

    def test_empty_option_list_is_handled(self) -> None:
        self.assertEqual(build_cost_guidance((), approver_id="M1"), ())


if __name__ == "__main__":
    unittest.main()
