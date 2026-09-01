"""预算账本：下单确认反过来喂管控。

第一组是账本本身的口径（只算回填过的、只算同币种、按下单时刻归期）；
第二组是接线：演示系统默认接账本，一单确认之后下一趟任务看见的余额就变了，
而上一趟任务钉住的快照一个字不变。
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    BookingConfirmationSource,
    PolicyOutcome,
    TaskState,
)
from corporate_travel_agent.domain.models import (
    BookingConfirmation,
    CostCenterBudget,
    EmployeeProfileSnapshot,
    LevelTravelRule,
    PolicySnapshot,
    TripTask,
)
from corporate_travel_agent.planning.cost_guidance import build_cost_guidance
from corporate_travel_agent.services.budget_ledger import (
    RepositoryTripBudgetLedger,
    derive_budget_snapshot,
)
from corporate_travel_agent.services.repositories import InMemoryTaskRepository

NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _employee(employee_id: str, cost_center: str | None) -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id=f"emp-{employee_id}",
        employee_id=employee_id,
        level="L3",
        department="Sales",
        home_city="Beijing",
        manager_id="M1",
        cost_center=cost_center,
    )


def _confirmed_task(
    task_id: str,
    *,
    cost_center: str | None,
    amount: str,
    currency: str = "USD",
    booked_at: datetime = NOW,
    state: TaskState = TaskState.BOOKING_CONFIRMED,
) -> TripTask:
    return TripTask(
        task_id=task_id,
        state=state,
        request=None,
        employee=_employee("E1", cost_center),
        policy_snapshot_id="pol",
        booking_confirmation=BookingConfirmation(
            confirmation_id=f"c-{task_id}",
            intent_id="i",
            option_id="o",
            option_version=1,
            order_references=("PNR",),
            total_amount=Decimal(amount),
            currency=currency,
            source=BookingConfirmationSource.SELF_REPORTED,
            reported_by="E1",
            reported_at=booked_at,
            booked_at=booked_at,
        ),
    )


def _policy_with_budget(amount: str = "20000") -> PolicySnapshot:
    return PolicySnapshot(
        snapshot_id="pol",
        policy_version="v",
        level_rules={"L3": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
        hotel_city_caps={},
        arrival_buffer_minutes=0,
        exception_allowed_rule_ids=frozenset(),
        effective_from=date(2026, 1, 1),
        cost_center_budgets={
            "CC-1": CostCenterBudget(
                cost_center="CC-1",
                amount=Decimal(amount),
                period_from=date(2026, 1, 1),
                period_to=date(2026, 12, 31),
                currency="USD",
            )
        },
    )


class LedgerTests(unittest.TestCase):
    def test_sums_only_confirmed_same_currency_bookings_in_the_period(self) -> None:
        repository = InMemoryTaskRepository()
        for task in (
            _confirmed_task("in-1", cost_center="CC-1", amount="1200"),
            _confirmed_task("in-2", cost_center="CC-1", amount="300.50"),
            # 只点了"我去订了"、没回填：没有金额，不算。
            _confirmed_task("handed", cost_center="CC-1", amount="999", state=TaskState.HANDED_OFF),
            # 别的成本中心。
            _confirmed_task("other-cc", cost_center="CC-2", amount="5000"),
            # 人民币：不换算，也不算进美元预算。
            _confirmed_task("cny", cost_center="CC-1", amount="8000", currency="CNY"),
            # 上一个预算期。
            _confirmed_task(
                "last-year", cost_center="CC-1", amount="700",
                booked_at=NOW - timedelta(days=400),
            ),
            _confirmed_task("no-cc", cost_center=None, amount="100"),
        ):
            repository.add(task)

        spent = RepositoryTripBudgetLedger(repository).spent(
            "CC-1", currency="USD", period_from=date(2026, 1, 1), period_to=date(2026, 12, 31)
        )

        self.assertEqual(spent, Decimal("1500.50"))

    def test_snapshot_is_none_without_a_cost_center_or_a_configured_budget(self) -> None:
        ledger = RepositoryTripBudgetLedger(InMemoryTaskRepository())
        self.assertIsNone(
            derive_budget_snapshot(_employee("E1", None), _policy_with_budget(), ledger, now=NOW)
        )
        no_budget = PolicySnapshot(
            snapshot_id="p",
            policy_version="v",
            level_rules={"L3": LevelTravelRule(("ECONOMY",), ("SECOND_CLASS",))},
            hotel_city_caps={},
            arrival_buffer_minutes=0,
            exception_allowed_rule_ids=frozenset(),
            effective_from=date(2026, 1, 1),
        )
        self.assertIsNone(
            derive_budget_snapshot(_employee("E1", "CC-1"), no_budget, ledger, now=NOW)
        )

    def test_snapshot_carries_limit_spent_and_a_content_bound_id(self) -> None:
        repository = InMemoryTaskRepository()
        repository.add(_confirmed_task("t1", cost_center="CC-1", amount="4000"))
        ledger = RepositoryTripBudgetLedger(repository)

        employee = _employee("E1", "CC-1")
        first = derive_budget_snapshot(employee, _policy_with_budget(), ledger, now=NOW)
        again = derive_budget_snapshot(employee, _policy_with_budget(), ledger, now=NOW)
        repository.add(_confirmed_task("t2", cost_center="CC-1", amount="1000"))
        later = derive_budget_snapshot(employee, _policy_with_budget(), ledger, now=NOW)

        assert first is not None and later is not None and again is not None
        self.assertEqual(first.limit, Decimal("20000"))
        self.assertEqual(first.spent, Decimal("4000"))
        self.assertEqual(first.remaining, Decimal("16000"))
        self.assertEqual(first.snapshot_id, again.snapshot_id)
        self.assertNotEqual(first.snapshot_id, later.snapshot_id)
        self.assertEqual(later.spent, Decimal("5000"))


class WiringTests(unittest.TestCase):
    """演示系统默认接账本：演示政策 v2 给 CC-SALES-CN 配了 20,000 美元。"""

    def _compliant(self, task):
        return next(
            item for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )

    def _budget_rule(self, option):
        return next(
            item for item in option.policy_decision.evidence
            if item.rule_id == "budget.cost_center.remaining"
        )

    def test_the_demo_pins_a_budget_snapshot_and_every_option_is_judged_against_it(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        task = workflow.create_task(make_demo_request(task_id="budget-first"))

        pinned = task.metadata["budget_snapshot"]
        self.assertEqual(pinned["cost_center"], "CC-SALES-CN")
        self.assertEqual(Decimal(pinned["limit"]), Decimal("20000"))
        self.assertEqual(Decimal(pinned["spent"]), Decimal("0"))
        for option in task.options:
            self.assertIs(self._budget_rule(option).outcome, PolicyOutcome.COMPLIANT)
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        self.assertIn("BUDGET_SNAPSHOT_PINNED", events)

    def test_a_confirmed_booking_shrinks_the_next_budget_and_routes_to_approval(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        first = workflow.create_task(make_demo_request(task_id="budget-spender"))
        option = self._compliant(first)
        workflow.select_option(first.task_id, option.option_id)
        workflow.confirm_booking(
            first.task_id,
            order_references=["PNR-BIG"],
            total_amount=Decimal("19000"),
            currency=option.currency,
            reported_by="E1001",
        )

        second = workflow.create_task(make_demo_request(task_id="budget-next"))

        pinned = second.metadata["budget_snapshot"]
        self.assertEqual(Decimal(pinned["spent"]), Decimal("19000"))
        self.assertEqual(Decimal(pinned["limit"]) - Decimal(pinned["spent"]), Decimal("1000"))
        over = [
            item for item in second.options
            if self._budget_rule(item).outcome is PolicyOutcome.REQUIRES_APPROVAL
        ]
        self.assertTrue(over, "demo trips cost more than 1,000 USD; they must need approval now")
        for item in over:
            self.assertEqual(self._budget_rule(item).amount_currency, option.currency)
        guidance = build_cost_guidance(second.options, approver_id="M2001")
        budget_overages = [
            overage
            for item in guidance
            for overage in item.policy_overages
            if overage.rule_id == "budget.cost_center.remaining"
        ]
        self.assertTrue(budget_overages)
        self.assertTrue(all(item.unit == "total" for item in budget_overages))

        # 第一趟任务钉住的快照一个字不变：它是"当初为什么这么判"的依据。
        stored_first = workflow.tasks.get(first.task_id)
        self.assertEqual(Decimal(stored_first.metadata["budget_snapshot"]["spent"]), Decimal("0"))

    def test_without_a_ledger_the_rule_cannot_be_judged_and_the_task_still_plans(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        workflow.budget_ledger = None

        task = workflow.create_task(make_demo_request(task_id="budget-no-ledger"))

        self.assertNotIn("budget_snapshot", task.metadata)
        self.assertTrue(task.options)
        for option in task.options:
            self.assertIs(self._budget_rule(option).outcome, PolicyOutcome.INSUFFICIENT_EVIDENCE)

    def test_a_broken_ledger_does_not_fail_the_task(self) -> None:
        class _Broken:
            def spent(self, *args, **kwargs):
                raise RuntimeError("ledger down")

        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK, budget_ledger=_Broken())

        task = workflow.create_task(make_demo_request(task_id="budget-broken"))

        self.assertTrue(task.options)
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        self.assertIn("BUDGET_SNAPSHOT_UNAVAILABLE", events)
        for option in task.options:
            self.assertIs(self._budget_rule(option).outcome, PolicyOutcome.INSUFFICIENT_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
