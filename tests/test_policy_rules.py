"""政策引擎后加的三个维度：提前预订天数、淡旺季夜费上限、成本中心预算余额。

每条规则都有"不判"和"判不了"两种口径，**它们不是一回事**：政策没配这条规则、或者
调用方没给判定所需的输入（只想看一张报价本身），就不产证据；配了规则却拿不到数据
（预算账本没接上），才是"判不了"，摆出来请人定。
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from corporate_travel_agent.domain.enums import PolicyOutcome, TransportMode
from corporate_travel_agent.domain.models import (
    BudgetSnapshot,
    CostCenterBudget,
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    SeasonalHotelCap,
    TransportOffer,
)
from corporate_travel_agent.policy.engine import PolicyEngine

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 1, 21, 0, tzinfo=SHANGHAI)
ALL_EXCEPTIONS = frozenset(
    {
        "hotel.city.nightly_cap",
        "hotel.city.seasonal_cap",
        "booking.advance_days",
        "budget.cost_center.remaining",
    }
)


def _employee(cost_center: str | None = None) -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="emp",
        employee_id="E1",
        level="L3",
        department="Sales",
        home_city="Beijing",
        manager_id="M1",
        cost_center=cost_center,
    )


def _policy(**overrides) -> PolicySnapshot:
    values = dict(
        snapshot_id="pol",
        policy_version="v-test",
        level_rules={"L3": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        hotel_city_caps={"Shanghai": Decimal("600")},
        arrival_buffer_minutes=0,
        exception_allowed_rule_ids=ALL_EXCEPTIONS,
        effective_from=date(2026, 1, 1),
        currency="USD",
    )
    values.update(overrides)
    return PolicySnapshot(**values)


def _flight(depart: datetime, *, price: str = "950", ref: str = "MU-1") -> TransportOffer:
    return TransportOffer(
        ref_id=ref,
        snapshot_id="snap",
        provider="mock",
        mode=TransportMode.FLIGHT,
        origin="Beijing",
        destination="Shanghai",
        depart_at=depart,
        arrive_at=depart + timedelta(hours=2),
        price=Decimal(price),
        seat_class="ECONOMY",
        currency="USD",
    )


def _hotel(check_in: date, *, nightly: str = "520", currency: str = "USD") -> HotelOffer:
    return HotelOffer(
        ref_id="HT-1",
        snapshot_id="snap",
        provider="mock",
        name="Bund Inn",
        city="Shanghai",
        check_in=check_in,
        check_out=check_in + timedelta(days=1),
        nightly_price=Decimal(nightly),
        commute_minutes=10,
        currency=currency,
    )


def _rule(decision, rule_id: str):
    return next((item for item in decision.evidence if item.rule_id == rule_id), None)


class AdvanceDaysTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PolicyEngine()

    def test_no_rule_when_the_policy_sets_no_minimum(self) -> None:
        decision = self.engine.evaluate(
            _employee(), _policy(), [_flight(NOW + timedelta(days=1))], None, now=NOW
        )
        self.assertIsNone(_rule(decision, "booking.advance_days"))

    def test_not_judged_when_the_planning_time_is_unknown(self) -> None:
        """只看一张报价本身的诊断调用没有 now：不判，也不算判不了。"""
        decision = self.engine.evaluate(
            _employee(), _policy(min_advance_booking_days=3), [_flight(NOW)], None
        )
        self.assertIsNone(_rule(decision, "booking.advance_days"))

    def test_counts_calendar_days_in_the_departure_timezone(self) -> None:
        """8 月 1 日晚上订 8 月 4 日早上的票：提前 3 天，哪怕不足 72 小时。"""
        depart = datetime(2026, 8, 4, 6, 0, tzinfo=SHANGHAI)
        decision = self.engine.evaluate(
            _employee(), _policy(min_advance_booking_days=3), [_flight(depart)], None, now=NOW
        )
        evidence = _rule(decision, "booking.advance_days")
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.actual, "3 days")
        self.assertEqual(evidence.threshold, ">= 3 days")
        self.assertIs(evidence.outcome, PolicyOutcome.COMPLIANT)
        # 天数不是钱：不填数值字段，成本引导不会把它当成差额去显示。
        self.assertIsNone(evidence.overage_amount)

    def test_below_the_minimum_routes_to_approval(self) -> None:
        depart = datetime(2026, 8, 2, 6, 0, tzinfo=SHANGHAI)
        decision = self.engine.evaluate(
            _employee(), _policy(min_advance_booking_days=3), [_flight(depart)], None, now=NOW
        )
        evidence = _rule(decision, "booking.advance_days")
        self.assertIs(evidence.outcome, PolicyOutcome.REQUIRES_APPROVAL)
        self.assertTrue(evidence.exception_allowed)
        self.assertIs(decision.outcome, PolicyOutcome.REQUIRES_APPROVAL)

    def test_below_the_minimum_is_forbidden_when_no_exception_is_allowed(self) -> None:
        depart = datetime(2026, 8, 2, 6, 0, tzinfo=SHANGHAI)
        policy = _policy(min_advance_booking_days=3, exception_allowed_rule_ids=frozenset())
        decision = self.engine.evaluate(_employee(), policy, [_flight(depart)], None, now=NOW)
        self.assertIs(_rule(decision, "booking.advance_days").outcome, PolicyOutcome.FORBIDDEN)

    def test_the_earliest_leg_decides(self) -> None:
        legs = [
            _flight(NOW + timedelta(days=10), ref="LATE"),
            _flight(datetime(2026, 8, 2, 6, 0, tzinfo=SHANGHAI), ref="SOON"),
        ]
        decision = self.engine.evaluate(
            _employee(), _policy(min_advance_booking_days=3), legs, None, now=NOW
        )
        evidence = _rule(decision, "booking.advance_days")
        self.assertEqual(evidence.actual, "1 days")
        self.assertIn("SOON", evidence.message)


class SeasonalCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PolicyEngine()
        self.expo = SeasonalHotelCap(
            city="Shanghai",
            season_from=date(2026, 8, 1),
            season_to=date(2026, 8, 31),
            nightly_cap=Decimal("450"),
            label="expo",
        )

    def test_in_season_the_seasonal_cap_replaces_the_base_cap(self) -> None:
        policy = _policy(hotel_seasonal_caps=(self.expo,))
        decision = self.engine.evaluate(
            _employee(), policy, [], _hotel(date(2026, 8, 5), nightly="520")
        )
        seasonal = _rule(decision, "hotel.city.seasonal_cap")
        self.assertIsNotNone(seasonal)
        self.assertIsNone(_rule(decision, "hotel.city.nightly_cap"))
        self.assertIs(seasonal.outcome, PolicyOutcome.REQUIRES_APPROVAL)
        self.assertEqual(seasonal.overage_amount, Decimal("70"))
        self.assertEqual(seasonal.amount_currency, "USD")
        self.assertIn("expo", seasonal.threshold)

    def test_out_of_season_the_base_cap_applies(self) -> None:
        policy = _policy(hotel_seasonal_caps=(self.expo,))
        decision = self.engine.evaluate(
            _employee(), policy, [], _hotel(date(2026, 9, 5), nightly="520")
        )
        self.assertIsNone(_rule(decision, "hotel.city.seasonal_cap"))
        self.assertIs(_rule(decision, "hotel.city.nightly_cap").outcome, PolicyOutcome.COMPLIANT)

    def test_overlapping_windows_pick_the_stricter_cap(self) -> None:
        loose = SeasonalHotelCap(
            city="Shanghai",
            season_from=date(2026, 8, 1),
            season_to=date(2026, 8, 31),
            nightly_cap=Decimal("700"),
            label="summer",
        )
        policy = _policy(hotel_seasonal_caps=(loose, self.expo))
        decision = self.engine.evaluate(
            _employee(), policy, [], _hotel(date(2026, 8, 5), nightly="520")
        )
        seasonal = _rule(decision, "hotel.city.seasonal_cap")
        self.assertEqual(seasonal.threshold_amount, Decimal("450"))

    def test_a_season_can_also_raise_the_cap(self) -> None:
        peak = SeasonalHotelCap(
            city="Shanghai",
            season_from=date(2026, 8, 1),
            season_to=date(2026, 8, 31),
            nightly_cap=Decimal("800"),
            label="peak",
        )
        policy = _policy(hotel_seasonal_caps=(peak,))
        decision = self.engine.evaluate(
            _employee(), policy, [], _hotel(date(2026, 8, 5), nightly="720")
        )
        self.assertIs(_rule(decision, "hotel.city.seasonal_cap").outcome, PolicyOutcome.COMPLIANT)


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PolicyEngine()
        self.budget_policy = _policy(
            cost_center_budgets={
                "CC-1": CostCenterBudget(
                    cost_center="CC-1",
                    amount=Decimal("20000"),
                    period_from=date(2026, 1, 1),
                    period_to=date(2026, 12, 31),
                    currency="USD",
                )
            }
        )
        self.trip = ([_flight(NOW + timedelta(days=5))], _hotel(date(2026, 8, 6)))

    def _snapshot(self, spent: str, cost_center: str = "CC-1") -> BudgetSnapshot:
        return BudgetSnapshot(
            snapshot_id="b",
            cost_center=cost_center,
            currency="USD",
            limit=Decimal("20000"),
            spent=Decimal(spent),
            period_from=date(2026, 1, 1),
            period_to=date(2026, 12, 31),
            computed_at=NOW,
        )

    def test_no_rule_without_a_cost_center_or_a_configured_budget_or_the_flag(self) -> None:
        legs, hotel = self.trip
        cases = [
            (_employee(None), self.budget_policy, True),
            (_employee("CC-1"), _policy(), True),
            (_employee("CC-1"), self.budget_policy, False),
        ]
        for employee, policy, assess in cases:
            decision = self.engine.evaluate(
                employee, policy, legs, hotel, budget=self._snapshot("0"), assess_budget=assess
            )
            self.assertIsNone(_rule(decision, "budget.cost_center.remaining"))

    def test_a_configured_budget_without_a_snapshot_cannot_be_judged(self) -> None:
        legs, hotel = self.trip
        decision = self.engine.evaluate(
            _employee("CC-1"), self.budget_policy, legs, hotel, budget=None, assess_budget=True
        )
        evidence = _rule(decision, "budget.cost_center.remaining")
        self.assertIs(evidence.outcome, PolicyOutcome.INSUFFICIENT_EVIDENCE)
        self.assertIn("budget.cost_center.remaining", decision.unjudged_rule_ids)

    def test_within_the_remaining_budget_is_compliant(self) -> None:
        legs, hotel = self.trip
        decision = self.engine.evaluate(
            _employee("CC-1"),
            self.budget_policy,
            legs,
            hotel,
            budget=self._snapshot("18000"),
            assess_budget=True,
        )
        evidence = _rule(decision, "budget.cost_center.remaining")
        self.assertIs(evidence.outcome, PolicyOutcome.COMPLIANT)
        self.assertEqual(evidence.actual_amount, Decimal("1470"))
        self.assertEqual(evidence.threshold_amount, Decimal("2000"))
        self.assertEqual(evidence.overage_amount, Decimal("0"))

    def test_over_the_remaining_budget_routes_to_approval_with_the_overage(self) -> None:
        legs, hotel = self.trip
        decision = self.engine.evaluate(
            _employee("CC-1"),
            self.budget_policy,
            legs,
            hotel,
            budget=self._snapshot("19500"),
            assess_budget=True,
        )
        evidence = _rule(decision, "budget.cost_center.remaining")
        self.assertIs(evidence.outcome, PolicyOutcome.REQUIRES_APPROVAL)
        self.assertEqual(evidence.overage_amount, Decimal("970"))
        self.assertEqual(evidence.amount_currency, "USD")

    def test_an_exhausted_budget_reports_a_zero_threshold_not_a_negative_one(self) -> None:
        legs, hotel = self.trip
        decision = self.engine.evaluate(
            _employee("CC-1"),
            self.budget_policy,
            legs,
            hotel,
            budget=self._snapshot("20500"),
            assess_budget=True,
        )
        evidence = _rule(decision, "budget.cost_center.remaining")
        self.assertEqual(evidence.threshold_amount, Decimal("0"))
        self.assertEqual(evidence.overage_amount, Decimal("1470"))
        self.assertIn("-500", evidence.threshold)

    def test_over_budget_is_forbidden_when_no_exception_is_allowed(self) -> None:
        legs, hotel = self.trip
        policy = _policy(
            cost_center_budgets=self.budget_policy.cost_center_budgets,
            exception_allowed_rule_ids=frozenset(),
        )
        decision = self.engine.evaluate(
            _employee("CC-1"),
            policy,
            legs,
            hotel,
            budget=self._snapshot("19500"),
            assess_budget=True,
        )
        budget_rule = _rule(decision, "budget.cost_center.remaining")
        self.assertIs(budget_rule.outcome, PolicyOutcome.FORBIDDEN)

    def test_a_snapshot_for_another_cost_center_is_not_borrowed(self) -> None:
        legs, hotel = self.trip
        decision = self.engine.evaluate(
            _employee("CC-1"),
            self.budget_policy,
            legs,
            hotel,
            budget=self._snapshot("0", cost_center="CC-OTHER"),
            assess_budget=True,
        )
        self.assertIs(
            _rule(decision, "budget.cost_center.remaining").outcome,
            PolicyOutcome.INSUFFICIENT_EVIDENCE,
        )

    def test_mixed_currencies_leave_the_budget_rule_out(self) -> None:
        """币种对不上已经由 pricing.currency 判成"判不了"，预算不再叠一条。"""
        legs, _ = self.trip
        decision = self.engine.evaluate(
            _employee("CC-1"),
            self.budget_policy,
            legs,
            _hotel(date(2026, 8, 6), currency="CNY"),
            budget=self._snapshot("0"),
            assess_budget=True,
        )
        self.assertIsNone(_rule(decision, "budget.cost_center.remaining"))
        currency_rule = _rule(decision, "pricing.currency")
        self.assertIs(currency_rule.outcome, PolicyOutcome.INSUFFICIENT_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
