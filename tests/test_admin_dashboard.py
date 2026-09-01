"""管理端看板的三块数据：业务指标、谁在哪（duty of care）、预算消耗。

守住的东西：
- 谁在哪只用确认过的行程；没确认的不出现，观察期过了默认不出现；
- 在途 / 在目的地 / 未出发按航段时刻判，改期未订好的标出来；
- 预算行区分账本里的支出和已交接未确认的在途金额；
- 三个接口都只对管理员开放。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TripEventType
from corporate_travel_agent.services.duty_of_care import (
    WhereaboutsStatus,
    whereabouts,
    whereabouts_of,
)


def _book(workflow, task_id: str):
    task = workflow.create_task(make_demo_request(task_id=task_id))
    option = next(
        item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(task.task_id, option.option_id)
    workflow.confirm_booking(
        task.task_id,
        order_references=[f"PNR-{task_id}"],
        total_amount=option.total_cost,
        currency=option.currency,
        reported_by="E1001",
    )
    return workflow.tasks.get(task.task_id), option


class WhereaboutsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)

    def test_unconfirmed_trips_are_not_located(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="dc-planned"))
        trip = self.workflow.trips.get(task.trip_id)
        self.assertIsNone(whereabouts_of(trip, at=DEMO_CLOCK))
        self.assertEqual(whereabouts([trip], at=DEMO_CLOCK), ())

    def test_status_follows_the_confirmed_legs(self) -> None:
        task, option = _book(self.workflow, "dc-booked")
        trip = self.workflow.trips.get(task.trip_id)
        leg = trip.watch.legs[0]

        before = whereabouts_of(trip, at=leg.depart_at - timedelta(hours=1))
        self.assertEqual(before.status, WhereaboutsStatus.UPCOMING)
        self.assertEqual(before.location, leg.origin)
        self.assertEqual(before.next_leg, leg)

        flying = whereabouts_of(trip, at=leg.depart_at + timedelta(minutes=10))
        self.assertEqual(flying.status, WhereaboutsStatus.IN_TRANSIT)
        self.assertEqual(flying.location, f"{leg.origin}→{leg.destination}")
        self.assertEqual(flying.current_leg, leg)

        # 演示方案是原路往返：落地后、回程起飞前，人在目的地，下一段是回程。
        legs = sorted(trip.watch.legs, key=lambda item: item.depart_at)
        landed = whereabouts_of(trip, at=leg.arrive_at + timedelta(hours=2))
        self.assertEqual(landed.status, WhereaboutsStatus.AT_DESTINATION)
        self.assertEqual(landed.location, leg.destination)
        self.assertEqual(landed.next_leg, legs[1] if len(legs) > 1 else None)
        self.assertFalse(landed.change_pending)

        home = whereabouts_of(trip, at=legs[-1].arrive_at + timedelta(hours=1))
        self.assertEqual(home.status, WhereaboutsStatus.AT_DESTINATION)
        self.assertEqual(home.location, legs[-1].destination)
        self.assertIsNone(home.next_leg)

        done = whereabouts_of(trip, at=trip.watch.watch_until + timedelta(seconds=1))
        self.assertEqual(done.status, WhereaboutsStatus.COMPLETED)
        self.assertEqual(whereabouts([trip], at=done.watch_until + timedelta(days=1)), ())
        self.assertEqual(
            whereabouts([trip], at=done.watch_until + timedelta(days=1), include_completed=True)[
                0
            ].status,
            WhereaboutsStatus.COMPLETED,
        )

    def test_change_pending_is_flagged_and_in_transit_sorts_first(self) -> None:
        first, option = _book(self.workflow, "dc-first")
        second, _ = _book(self.workflow, "dc-second")
        self.workflow.report_trip_event(
            first.trip_id,
            event_type=TripEventType.FLIGHT_CHANGED,
            ref_id=option.legs[0].ref_id,
            reported_by="carrier-feed",
        )
        trips = self.workflow.trips.list_all()
        leg = self.workflow.trips.get(second.trip_id).watch.legs[0]
        rows = whereabouts(trips, at=leg.depart_at + timedelta(minutes=5))
        self.assertEqual([item.status for item in rows], [WhereaboutsStatus.IN_TRANSIT] * 2)
        flagged = {item.trip_id: item.change_pending for item in rows}
        self.assertTrue(flagged[first.trip_id])
        self.assertFalse(flagged[second.trip_id])


class DashboardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from corporate_travel_agent.api import main as api_main

        self.api_main = api_main
        self.client = TestClient(api_main.app)
        self._previous_clock = api_main.workflow.clock
        api_main.workflow.clock = lambda: DEMO_CLOCK

    def tearDown(self) -> None:
        self.api_main.workflow.clock = self._previous_clock
        self.api_main.app.dependency_overrides.pop(self.api_main._current_identity, None)

    def _as_employee(self) -> None:
        from corporate_travel_agent.services.auth import Role, UserIdentity

        self.api_main.app.dependency_overrides[self.api_main._current_identity] = (
            lambda: UserIdentity(
                user_id="user-E1001", roles=frozenset({Role.EMPLOYEE}), employee_id="E1001"
            )
        )

    def test_dashboard_endpoints_are_admin_only(self) -> None:
        self._as_employee()
        for path in ("/metrics/business", "/duty-of-care", "/budgets"):
            self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_duty_of_care_and_budgets_reflect_confirmations(self) -> None:
        workflow = self.api_main.workflow
        task, option = _book(workflow, "dc-api")
        leg = workflow.trips.get(task.trip_id).watch.legs[0]

        located = self.client.get(
            "/duty-of-care", params={"at": (leg.depart_at + timedelta(minutes=1)).isoformat()}
        )
        self.assertEqual(located.status_code, 200, located.text)
        row = next(item for item in located.json()["travelers"] if item["trip_id"] == task.trip_id)
        self.assertEqual(row["status"], "IN_TRANSIT")
        self.assertEqual(row["traveler_id"], "E1001")
        self.assertEqual(row["current_leg"]["ref_id"], leg.ref_id)

        budgets = self.client.get("/budgets")
        self.assertEqual(budgets.status_code, 200, budgets.text)
        lines = {item["cost_center"]: item for item in budgets.json()["budgets"]}
        self.assertIn("CC-SALES-CN", lines)
        line = lines["CC-SALES-CN"]
        self.assertTrue(line["ledger_available"])
        self.assertGreaterEqual(Decimal(line["spent"]), option.total_cost)
        self.assertEqual(
            Decimal(line["remaining"]), Decimal(line["limit"]) - Decimal(line["spent"])
        )

        # 已交接、没回填：进"在途"，不进"支出"。
        handed = workflow.create_task(make_demo_request(task_id="dc-handed"))
        compliant = next(
            item
            for item in handed.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        workflow.select_option(handed.task_id, compliant.option_id)
        after = {
            item["cost_center"]: item for item in self.client.get("/budgets").json()["budgets"]
        }["CC-SALES-CN"]
        self.assertGreaterEqual(Decimal(after["committed"]), compliant.total_cost)
        self.assertEqual(after["spent"], line["spent"])

        metrics = self.client.get("/metrics/business").json()
        self.assertIn("booking_confirmation_rate", metrics["metrics"])
        self.assertEqual(metrics["labels"]["change_intervention_rate"][:4], "改期任务")


if __name__ == "__main__":
    unittest.main()
