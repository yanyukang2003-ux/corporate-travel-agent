"""一趟差旅：规划任务建它，下单确认登记观察对象，变更事件开改期任务、原任务不动。

守住的东西：
- 改期是**新任务**——原任务的状态和审计一个字不动；
- 事件只在"已订、观察期没过、对得上票"时被接受，其余一律拒绝；
- 航变把那张票排除在候选之外，会议改期改最晚到达时刻；
- 改期任务确认后差旅进入 REBOOKED，观察对象换成新任务的航段；
- 内存和 SQL 两种仓储行为一致；
- 变更场景人工介入率从改期任务的审计里算出来。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import create_engine

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, TripEventType, TripStatus
from corporate_travel_agent.services.evaluation_business import aggregate_metrics, summarize_task
from corporate_travel_agent.services.repositories import ConcurrentUpdateError, NotFoundError
from corporate_travel_agent.services.sqlalchemy_repository import Base
from corporate_travel_agent.services.trips import (
    InMemoryTripRepository,
    SQLAlchemyTripRepository,
)


class _Clock:
    def __init__(self) -> None:
        self.now = DEMO_CLOCK

    def __call__(self):
        return self.now


class TripLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.workflow, _ = build_demo_system(clock=self.clock)

    def _booked_task(self, task_id: str = "trip-original"):
        task = self.workflow.create_task(make_demo_request(task_id=task_id))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(task.task_id, option.option_id)
        task = self.workflow.confirm_booking(
            task.task_id,
            order_references=["PNR-1"],
            total_amount=option.total_cost,
            currency=option.currency,
            reported_by="E1001",
        )
        return task, option

    def _event_types(self, task_id: str) -> list[str]:
        return [item.event_type for item in self.workflow.tasks.events(task_id)]

    def test_creating_a_task_opens_a_planned_trip(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="trip-planned"))
        self.assertIsNotNone(task.trip_id)
        trip = self.workflow.trips.get(task.trip_id)
        self.assertEqual(trip.status, TripStatus.PLANNED)
        self.assertEqual(trip.task_ids, ("trip-planned",))
        self.assertEqual(trip.traveler_id, "E1001")
        self.assertIsNone(trip.watch)
        self.assertIs(self.workflow.trips.find_by_task("trip-planned"), trip)
        self.assertFalse(task.is_change_task)

    def test_confirming_a_booking_registers_the_watch(self) -> None:
        task, option = self._booked_task()
        trip = self.workflow.trips.get(task.trip_id)
        self.assertEqual(trip.status, TripStatus.BOOKED)
        self.assertIsNotNone(trip.watch)
        self.assertEqual(trip.watch.task_id, task.task_id)
        self.assertEqual(
            [leg.ref_id for leg in trip.watch.legs], [leg.ref_id for leg in option.legs]
        )
        last_arrival = max(leg.arrive_at for leg in trip.watch.legs)
        self.assertEqual(trip.watch.watch_until, last_arrival + timedelta(days=1))

    def test_a_flight_change_opens_a_change_task_and_leaves_the_original_alone(self) -> None:
        task, option = self._booked_task()
        original_events = self._event_types(task.task_id)
        cancelled = option.legs[0].ref_id

        change = self.workflow.report_trip_event(
            task.trip_id,
            event_type=TripEventType.FLIGHT_CHANGED,
            ref_id=cancelled,
            note="航司取消了这一班",
            reported_by="carrier-feed",
        )

        self.assertTrue(change.is_change_task)
        self.assertEqual(change.parent_task_id, task.task_id)
        self.assertEqual(change.trip_id, task.trip_id)
        self.assertEqual(change.state, TaskState.WAITING_FOR_USER)
        self.assertTrue(change.options)
        offered = {leg.ref_id for item in change.options for leg in item.legs}
        self.assertNotIn(cancelled, offered)
        self.assertEqual(change.metadata["change_event"]["event_type"], "FLIGHT_CHANGED")
        self.assertEqual(change.metadata["change_event"]["excluded_refs"], [cancelled])

        # 原任务：状态、审计一个字不动。
        original = self.workflow.tasks.get(task.task_id)
        self.assertEqual(original.state, TaskState.BOOKING_CONFIRMED)
        self.assertEqual(self._event_types(task.task_id), original_events)

        trip = self.workflow.trips.get(task.trip_id)
        self.assertEqual(trip.status, TripStatus.CHANGE_REQUESTED)
        self.assertEqual(trip.task_ids, (task.task_id, change.task_id))
        self.assertEqual(len(trip.events), 1)
        self.assertEqual(trip.events[0].opened_task_id, change.task_id)
        self.assertEqual(trip.events[0].ref_id, cancelled)
        self.assertEqual(change.change_event_id, trip.events[0].event_id)

        pending = self.workflow.tasks.outbox.list_unpublished(limit=100)
        self.assertIn("TRIP_CHANGE_REQUESTED", {item.event_type for item in pending})
        self.assertIn("TASK_CREATED", self._event_types(change.task_id))

    def test_a_meeting_move_changes_the_arrive_by_and_may_be_reported_by_the_traveler(
        self,
    ) -> None:
        task, _ = self._booked_task()
        new_arrive_by = task.request.arrive_by + timedelta(days=1)

        change = self.workflow.report_trip_event(
            task.trip_id,
            event_type=TripEventType.MEETING_MOVED,
            new_arrive_by=new_arrive_by,
            reported_by="E1001",
        )

        self.assertEqual(change.request.arrive_by, new_arrive_by)
        # 出发窗口跟着到场时限一起平移：会议推迟一天，最早出发也推迟一天。
        self.assertEqual(
            change.request.departure_after, task.request.departure_after + timedelta(days=1)
        )
        self.assertEqual(change.request.version, 1)
        self.assertNotEqual(change.task_id, task.task_id)
        self.assertEqual(change.metadata["excluded_refs"], [])

    def test_confirming_the_change_task_rebooks_the_trip(self) -> None:
        task, _ = self._booked_task()
        # 会议改期不排除任何票，改期任务里仍有合规方案可选。
        change = self.workflow.report_trip_event(
            task.trip_id,
            event_type=TripEventType.MEETING_MOVED,
            new_arrive_by=task.request.arrive_by + timedelta(hours=6),
            reported_by="E1001",
        )
        new_option = next(
            item
            for item in change.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(change.task_id, new_option.option_id)
        self.workflow.confirm_booking(
            change.task_id,
            order_references=["PNR-2"],
            total_amount=new_option.total_cost,
            currency=new_option.currency,
            reported_by="E1001",
        )

        trip = self.workflow.trips.get(task.trip_id)
        self.assertEqual(trip.status, TripStatus.REBOOKED)
        self.assertEqual(trip.watch.task_id, change.task_id)
        self.assertEqual(
            [leg.ref_id for leg in trip.watch.legs], [leg.ref_id for leg in new_option.legs]
        )
        # 第二次航变仍然能报：观察对象已经换成新任务的航段。
        second = self.workflow.report_trip_event(
            task.trip_id,
            event_type=TripEventType.FLIGHT_CHANGED,
            ref_id=new_option.legs[0].ref_id,
            reported_by="carrier-feed",
        )
        self.assertEqual(second.parent_task_id, change.task_id)

    def test_events_are_refused_unless_the_trip_is_booked_and_watched(self) -> None:
        planned = self.workflow.create_task(make_demo_request(task_id="trip-not-booked"))
        with self.assertRaisesRegex(WorkflowError, "no confirmed booking"):
            self.workflow.report_trip_event(
                planned.trip_id,
                event_type=TripEventType.MEETING_MOVED,
                new_arrive_by=DEMO_CLOCK + timedelta(days=3),
                reported_by="E1001",
            )

        task, option = self._booked_task()
        with self.assertRaisesRegex(WorkflowError, "not part of the booked trip"):
            self.workflow.report_trip_event(
                task.trip_id,
                event_type=TripEventType.FLIGHT_CHANGED,
                ref_id="NOT-A-TICKET",
                reported_by="carrier-feed",
            )
        with self.assertRaisesRegex(WorkflowError, "needs the ref_id"):
            self.workflow.report_trip_event(
                task.trip_id, event_type=TripEventType.FLIGHT_CHANGED, reported_by="carrier-feed"
            )
        with self.assertRaisesRegex(WorkflowError, "needs the new arrive-by"):
            self.workflow.report_trip_event(
                task.trip_id, event_type=TripEventType.MEETING_MOVED, reported_by="E1001"
            )
        with self.assertRaises(NotFoundError):
            self.workflow.report_trip_event(
                "no-such-trip", event_type=TripEventType.MEETING_MOVED, reported_by="E1001"
            )

        # 观察期过了：人已经飞完了，没什么可改的。
        trip = self.workflow.trips.get(task.trip_id)
        self.clock.now = trip.watch.watch_until + timedelta(minutes=1)
        with self.assertRaisesRegex(WorkflowError, "already been travelled"):
            self.workflow.report_trip_event(
                task.trip_id,
                event_type=TripEventType.FLIGHT_CHANGED,
                ref_id=option.legs[0].ref_id,
                reported_by="carrier-feed",
            )
        self.assertEqual(len(self.workflow.trips.get(task.trip_id).events), 0)

    def test_change_metrics_count_only_change_tasks(self) -> None:
        task, option = self._booked_task()
        change = self.workflow.report_trip_event(
            task.trip_id,
            event_type=TripEventType.FLIGHT_CHANGED,
            ref_id=option.legs[0].ref_id,
            reported_by="carrier-feed",
        )
        records = [
            summarize_task(item, self.workflow.tasks.events(item.task_id))
            for item in (self.workflow.tasks.get(task.task_id), change)
        ]
        self.assertEqual([item.is_change_task for item in records], [False, True])
        self.assertIsNone(records[0].change_auto_planned)
        self.assertTrue(records[1].change_auto_planned)
        self.assertFalse(records[1].change_needed_intervention)

        metrics = aggregate_metrics(records)
        self.assertEqual(metrics["change_auto_planned_rate"].value, 1.0)
        self.assertEqual(metrics["change_intervention_rate"].value, 0.0)
        self.assertEqual(metrics["change_intervention_rate"].status, "measured")

        # 人插手了：改需求。指标随之变化，且只看改期任务。
        self.workflow.revise_request(
            change.task_id,
            replace(
                change.request, version=2, arrive_by=change.request.arrive_by + timedelta(hours=2)
            ),
        )
        revised = self.workflow.tasks.get(change.task_id)
        record = summarize_task(revised, self.workflow.tasks.events(revised.task_id))
        self.assertTrue(record.change_needed_intervention)
        self.assertFalse(record.change_auto_planned)

    def test_metrics_without_change_tasks_are_not_applicable(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="trip-only"))
        records = [summarize_task(task, self.workflow.tasks.events(task.task_id))]
        metrics = aggregate_metrics(records)
        self.assertNotEqual(metrics["change_intervention_rate"].status, "measured")


class TripRepositoryTests(unittest.TestCase):
    def _trip(self):
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        task = workflow.create_task(make_demo_request(task_id="repo-trip"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        workflow.select_option(task.task_id, option.option_id)
        workflow.confirm_booking(
            task.task_id,
            order_references=["PNR-R"],
            total_amount=option.total_cost,
            currency=option.currency,
            reported_by="E1001",
        )
        return workflow.trips.get(task.trip_id)

    def _check(self, repository) -> None:
        trip = self._trip()
        repository.add(trip)
        with self.assertRaises(ValueError):
            repository.add(trip)
        loaded = repository.get(trip.trip_id)
        self.assertEqual(loaded.status, TripStatus.BOOKED)
        self.assertEqual(loaded.watch.legs, trip.watch.legs)
        self.assertEqual(loaded.watch.watch_until, trip.watch.watch_until)
        loaded.status = TripStatus.CHANGE_REQUESTED
        repository.save(loaded)
        self.assertEqual(repository.get(trip.trip_id).status, TripStatus.CHANGE_REQUESTED)
        self.assertEqual(repository.list_by_traveler("E1001")[0].trip_id, trip.trip_id)
        self.assertEqual(repository.list_involving("E1001")[0].trip_id, trip.trip_id)
        self.assertEqual(repository.list_by_traveler("NOBODY"), ())
        self.assertEqual(len(repository.list_all()), 1)
        self.assertEqual(repository.find_by_task("repo-trip").trip_id, trip.trip_id)
        self.assertIsNone(repository.find_by_task("no-such-task"))
        with self.assertRaises(NotFoundError):
            repository.get("missing")

    def test_in_memory_repository(self) -> None:
        self._check(InMemoryTripRepository())

    def test_sqlalchemy_repository_roundtrip_and_optimistic_lock(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        repository = SQLAlchemyTripRepository(engine)
        self._check(repository)
        trip = repository.list_all()[0]
        older = repository.get(trip.trip_id)
        trip.status = TripStatus.REBOOKED
        repository.save(trip)
        older.status = TripStatus.PLANNED
        with self.assertRaises(ConcurrentUpdateError):
            repository.save(older)
        self.assertEqual(repository.get(trip.trip_id).status, TripStatus.REBOOKED)


class TripApiTests(unittest.TestCase):
    """`/trips`：看差旅、报事件；航变只有管理员能报，会议改期旅行者自己能报。"""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from corporate_travel_agent.api import main as api_main

        self.app = api_main.app
        self.client = TestClient(api_main.app)
        # 演示行程在 2026 年 8 月；把 API 的时钟钉在演示时刻，不然全是"出发日已过"。
        self._previous_clock = api_main.workflow.clock
        api_main.workflow.clock = lambda: DEMO_CLOCK

    def tearDown(self) -> None:
        from corporate_travel_agent.api import main as api_main

        api_main.workflow.clock = self._previous_clock

    def _as_employee(self, employee_id: str) -> None:
        from corporate_travel_agent.api.main import _current_identity
        from corporate_travel_agent.services.auth import Role, UserIdentity

        self.app.dependency_overrides[_current_identity] = lambda: UserIdentity(
            user_id=f"user-{employee_id}", roles=frozenset({Role.EMPLOYEE}), employee_id=employee_id
        )

    def _booked_via_api(self) -> tuple[str, str]:
        created = self.client.post(
            "/trip-tasks",
            json={
                "traveler_id": "E1001", "origin": "Beijing", "destination": "Shanghai",
                "departure_after": "2026-08-05T05:00:00+08:00",
                "arrive_by": "2026-08-06T10:00:00+08:00",
            },
        ).json()
        compliant = next(o for o in created["options"] if o["policy_outcome"] == "COMPLIANT")
        task_id = created["task_id"]
        self.client.post(
            f"/trip-tasks/{task_id}/select-option", json={"option_id": compliant["option_id"]}
        )
        confirmed = self.client.post(
            f"/trip-tasks/{task_id}/booking-confirmation",
            json={"order_references": ["PNR-TRIP"], "total_amount": str(compliant["total_cost"]),
                  "currency": compliant["currency"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertIsNotNone(created["trip_id"])
        self.assertFalse(created["is_change_task"])
        return task_id, created["trip_id"]

    def test_flight_change_reported_by_admin_opens_a_linked_change_task(self) -> None:
        task_id, trip_id = self._booked_via_api()
        trip = self.client.get(f"/trips/{trip_id}").json()
        self.assertEqual(trip["status"], "BOOKED")
        self.assertEqual(trip["watch"]["task_id"], task_id)
        ref = trip["watch"]["legs"][0]["ref_id"]

        response = self.client.post(
            f"/trips/{trip_id}/events",
            json={"event_type": "FLIGHT_CHANGED", "ref_id": ref, "note": "航司取消"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        change = response.json()
        self.assertTrue(change["is_change_task"])
        self.assertEqual(change["parent_task_id"], task_id)
        self.assertEqual(change["trip_id"], trip_id)
        self.assertEqual(change["change_event"]["ref_id"], ref)
        self.assertEqual(change["state"], "WAITING_FOR_USER")

        trip = self.client.get(f"/trips/{trip_id}").json()
        self.assertEqual(trip["status"], "CHANGE_REQUESTED")
        self.assertEqual(trip["task_ids"], [task_id, change["task_id"]])
        self.assertEqual(trip["events"][0]["opened_task_id"], change["task_id"])
        listed = self.client.get("/trips").json()
        self.assertIn(trip_id, {item["trip_id"] for item in listed})
        original = self.client.get(f"/trip-tasks/{task_id}").json()
        self.assertEqual(original["state"], "BOOKING_CONFIRMED")

    def test_employees_report_meeting_moves_but_not_flight_changes(self) -> None:
        task_id, trip_id = self._booked_via_api()
        ref = self.client.get(f"/trips/{trip_id}").json()["watch"]["legs"][0]["ref_id"]
        self._as_employee("E1001")
        try:
            forbidden = self.client.post(
                f"/trips/{trip_id}/events", json={"event_type": "FLIGHT_CHANGED", "ref_id": ref}
            )
            self.assertEqual(forbidden.status_code, 403)
            incomplete = self.client.post(
                f"/trips/{trip_id}/events", json={"event_type": "MEETING_MOVED"}
            )
            self.assertIn(incomplete.status_code, {400, 409, 422})
            moved = self.client.post(
                f"/trips/{trip_id}/events",
                json={"event_type": "MEETING_MOVED", "new_arrive_by": "2026-08-07T10:00:00+08:00"},
            )
            self.assertEqual(moved.status_code, 200, moved.text)
            self.assertEqual(moved.json()["parent_task_id"], task_id)
            mine = self.client.get("/trips").json()
            self.assertIn(trip_id, {item["trip_id"] for item in mine})
            self._as_employee("E9999")
            self.assertEqual(self.client.get(f"/trips/{trip_id}").status_code, 404)
            self.assertEqual(self.client.get("/trips").json(), [])
        finally:
            from corporate_travel_agent.api.main import _current_identity

            self.app.dependency_overrides.pop(_current_identity, None)


if __name__ == "__main__":
    unittest.main()
