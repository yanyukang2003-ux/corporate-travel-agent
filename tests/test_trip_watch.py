"""订好之后的追踪与变更：watch worker、影响评估落地、取消差旅、订好之后的聊天改期。

守住的东西：
- 没接动态源时 worker 一趟都不领；接了就按节奏领，租约互斥，保存即释放；
- 延误但来得及只通知（审计 + 发件箱），同样的动态第二次不再通知；取消 / 赶不上才开改期任务；
- 动态源答不上来不当成航班有变；
- 取消差旅：观察停止、看板不显示、事件被拒、发件箱有通知；
- 会议改期可以指定是哪一段；
- 已订任务上的聊天读成变更请求，日期必须有旅行者原话出处；读不出来就问，任务状态不变。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine

from corporate_travel_agent.agent.orchestrator import LanguageModelUnavailable, WorkflowError
from corporate_travel_agent.agent.tool_loop import ModelTurn, ToolExchange, ToolInvocation, ToolSpec
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    ChangeImpactVerdict,
    FlightStatusKind,
    PolicyOutcome,
    TaskState,
    TripEventType,
    TripStatus,
)
from corporate_travel_agent.domain.models import FlightStatusReport
from corporate_travel_agent.providers.flight_status import (
    FlightStatusError,
    InMemoryFlightStatusSource,
    NullFlightStatusSource,
    flight_status_source_from_environment,
)
from corporate_travel_agent.services.duty_of_care import whereabouts
from corporate_travel_agent.services.sqlalchemy_repository import Base
from corporate_travel_agent.services.trips import SQLAlchemyTripRepository

TZ = ZoneInfo("Asia/Shanghai")


class _Clock:
    def __init__(self) -> None:
        self.now = DEMO_CLOCK

    def __call__(self):
        return self.now


class _FailingSource:
    name = "flaky"
    configured = True

    def status_of(self, leg, *, now):
        raise FlightStatusError("rate limited")


class ScriptedChangeModel:
    """按剧本吐工具调用的假模型；记下每一轮看到的对话、工具和上下文。"""

    prompt_version = "scripted-change-v1"

    def __init__(self, script: Sequence[tuple[str, dict[str, Any]]]) -> None:
        self._script = list(script)
        self.seen: list[dict[str, Any]] = []

    def next_turn(
        self,
        *,
        conversation: str,
        transcript: Sequence[ToolExchange],
        tools: Sequence[ToolSpec],
        context: Mapping[str, Any],
    ) -> ModelTurn:
        self.seen.append(
            {
                "conversation": conversation,
                "transcript": tuple(transcript),
                "tools": tuple(spec.name for spec in tools),
                "context": dict(context),
            }
        )
        if not self._script:
            return ModelTurn(message="我没听懂。")
        name, args = self._script.pop(0)
        return ModelTurn(calls=(ToolInvocation(name=name, arguments=args),))


def _report(ref_id, status, *, at, depart=None, arrive=None, source="memory", note=None):
    return FlightStatusReport(
        ref_id=ref_id,
        status=status,
        observed_at=at,
        source=source,
        estimated_depart_at=depart,
        estimated_arrive_at=arrive,
        note=note,
    )


class _Booked(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.source = InMemoryFlightStatusSource()
        self.workflow, _ = build_demo_system(
            clock=self.clock, flight_status_source=self.source, trip_watch_lease_seconds=300
        )

    def _booked_task(self, task_id: str = "watch-original"):
        task = self.workflow.create_task(make_demo_request(task_id=task_id))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(task.task_id, option.option_id)
        task = self.workflow.confirm_booking(
            task.task_id,
            order_references=["PNR-W"],
            total_amount=option.total_cost,
            currency=option.currency,
            reported_by="E1001",
        )
        return task, option

    def _events(self, task_id: str) -> list[str]:
        return [item.event_type for item in self.workflow.tasks.events(task_id)]

    def _outbox_types(self) -> list[str]:
        return [item.event_type for item in self.workflow.tasks.outbox.list_unpublished(limit=100)]


class WatchScheduleTests(_Booked):
    def test_confirming_schedules_the_first_check_48_hours_before_departure(self) -> None:
        task, option = self._booked_task()
        watch = self.workflow.trips.get(task.trip_id).watch
        first_departure = min(leg.depart_at for leg in option.legs)
        self.assertEqual(watch.next_check_at, first_departure - timedelta(hours=48))
        self.assertEqual(watch.check_count, 0)
        self.assertEqual(watch.observations, ())

    def test_a_null_source_claims_nothing(self) -> None:
        workflow, _ = build_demo_system(clock=self.clock)  # 默认没接动态源
        self.assertIsInstance(workflow.flight_status_source, NullFlightStatusSource)
        task = workflow.create_task(make_demo_request(task_id="null-src"))
        option = next(
            i for i in task.options if i.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        workflow.select_option(task.task_id, option.option_id)
        workflow.confirm_booking(
            task.task_id,
            order_references=["PNR-N"],
            total_amount=option.total_cost,
            currency=option.currency,
            reported_by="E1001",
        )
        self.clock.now = workflow.trips.get(task.trip_id).watch.next_check_at
        self.assertEqual(workflow.process_due_flight_checks(), ())
        self.assertEqual(workflow.trip_watch_metrics()["claimed"], 0)

    def test_source_from_environment(self) -> None:
        self.assertIsInstance(
            flight_status_source_from_environment({}), NullFlightStatusSource
        )
        self.assertIsInstance(
            flight_status_source_from_environment({"FLIGHT_STATUS_SOURCE": "memory"}),
            InMemoryFlightStatusSource,
        )
        with self.assertRaises(RuntimeError):
            flight_status_source_from_environment({"FLIGHT_STATUS_SOURCE": "variflight"})


class WatchWorkerTests(_Booked):
    def test_a_tolerable_delay_notifies_once_and_does_not_open_a_change_task(self) -> None:
        task, option = self._booked_task()
        trip_id = task.trip_id
        outbound = option.legs[0]
        watch = self.workflow.trips.get(trip_id).watch
        self.clock.now = watch.next_check_at
        self.source.record(
            _report(
                outbound.ref_id,
                FlightStatusKind.DELAYED,
                at=self.clock.now,
                arrive=outbound.arrive_at + timedelta(minutes=30),
            )
        )

        processed = self.workflow.process_due_flight_checks()

        self.assertEqual(processed, (trip_id,))
        # 只问了 48 小时内的那一段；返程还远，不问。
        self.assertEqual(self.source.queries, [outbound.ref_id])
        trip = self.workflow.trips.get(trip_id)
        self.assertEqual(trip.status, TripStatus.BOOKED)
        self.assertEqual(len(trip.watch.observations), 1)
        observation = trip.watch.observations[0]
        self.assertEqual(observation.verdict, ChangeImpactVerdict.NOTIFY_ONLY)
        self.assertIsNone(observation.opened_task_id)
        self.assertEqual(trip.watch.check_count, 1)
        self.assertGreater(trip.watch.next_check_at, self.clock.now)
        self.assertEqual(self._events(task.task_id).count("FLIGHT_STATUS_OBSERVED"), 1)
        self.assertIn("TRIP_FLIGHT_STATUS_NOTICE", self._outbox_types())
        self.assertEqual(self.workflow.tasks.get(task.task_id).state, TaskState.BOOKING_CONFIRMED)
        metrics = self.workflow.trip_watch_metrics()
        self.assertEqual((metrics["claimed"], metrics["checked"], metrics["notices"]), (1, 1, 1))

        # 同样的动态再来一次：不再通知、不再记审计。
        notices_before = self._outbox_types().count("TRIP_FLIGHT_STATUS_NOTICE")
        self.clock.now = trip.watch.next_check_at
        self.assertEqual(self.workflow.process_due_flight_checks(), (trip_id,))
        self.assertEqual(self._events(task.task_id).count("FLIGHT_STATUS_OBSERVED"), 1)
        self.assertEqual(self._outbox_types().count("TRIP_FLIGHT_STATUS_NOTICE"), notices_before)
        self.assertEqual(len(self.workflow.trips.get(trip_id).watch.observations), 1)

    def test_a_cancellation_opens_a_change_task_with_the_assessment_attached(self) -> None:
        task, option = self._booked_task()
        trip_id = task.trip_id
        outbound = option.legs[0]
        self.clock.now = self.workflow.trips.get(trip_id).watch.next_check_at
        self.source.record(_report(outbound.ref_id, FlightStatusKind.CANCELLED, at=self.clock.now))

        self.assertEqual(self.workflow.process_due_flight_checks(), (trip_id,))

        trip = self.workflow.trips.get(trip_id)
        self.assertEqual(trip.status, TripStatus.CHANGE_REQUESTED)
        observation = trip.watch.observation(outbound.ref_id)
        self.assertEqual(observation.verdict, ChangeImpactVerdict.REBOOK_REQUIRED)
        self.assertIsNotNone(observation.opened_task_id)
        change = self.workflow.tasks.get(observation.opened_task_id)
        self.assertTrue(change.is_change_task)
        self.assertEqual(change.parent_task_id, task.task_id)
        self.assertEqual(change.metadata["excluded_refs"], [outbound.ref_id])
        self.assertNotIn(
            outbound.ref_id, {leg.ref_id for item in change.options for leg in item.legs}
        )
        event = trip.events[0]
        self.assertEqual(event.event_type, TripEventType.FLIGHT_CHANGED)
        self.assertEqual(event.reported_by, "flight-status:memory")
        self.assertEqual(event.impact.verdict, ChangeImpactVerdict.REBOOK_REQUIRED)
        self.assertEqual(self.workflow.trip_watch_metrics()["changes_opened"], 1)
        self.assertIn("TRIP_CHANGE_REQUESTED", self._outbox_types())
        # 原任务状态不动；它只多了一条"收到航班动态"的审计。
        original = self.workflow.tasks.get(task.task_id)
        self.assertEqual(original.state, TaskState.BOOKING_CONFIRMED)
        self.assertIn("FLIGHT_STATUS_OBSERVED", self._events(task.task_id))
        # 改期进行中的差旅不再被领取。
        self.clock.now += timedelta(hours=6)
        self.assertEqual(self.workflow.process_due_flight_checks(), ())

    def test_a_delay_past_the_deadline_opens_a_change_task(self) -> None:
        task, option = self._booked_task()
        outbound = option.legs[0]
        self.clock.now = self.workflow.trips.get(task.trip_id).watch.next_check_at
        late = datetime(2026, 8, 6, 9, 30, tzinfo=TZ)  # 时限 10:00，缓冲 60 分钟
        self.source.record(
            _report(
                outbound.ref_id,
                FlightStatusKind.DELAYED,
                at=self.clock.now,
                depart=late - timedelta(hours=2),
                arrive=late,
            )
        )
        self.workflow.process_due_flight_checks()
        trip = self.workflow.trips.get(task.trip_id)
        self.assertEqual(trip.status, TripStatus.CHANGE_REQUESTED)
        self.assertEqual(trip.events[0].new_depart_at, late - timedelta(hours=2))
        self.assertIn("晚于最晚可接受到达", trip.events[0].impact.reasons[0])

    def test_a_source_failure_is_not_a_flight_change(self) -> None:
        task, _ = self._booked_task()
        workflow = self.workflow
        workflow.flight_status_source = _FailingSource()
        self.clock.now = workflow.trips.get(task.trip_id).watch.next_check_at
        self.assertEqual(workflow.process_due_flight_checks(), (task.trip_id,))
        trip = workflow.trips.get(task.trip_id)
        self.assertEqual(trip.status, TripStatus.BOOKED)
        self.assertEqual(trip.watch.observations, ())
        self.assertGreater(trip.watch.next_check_at, self.clock.now)
        self.assertEqual(workflow.trip_watch_metrics()["source_errors"], 1)
        self.assertNotIn("FLIGHT_STATUS_OBSERVED", self._events(task.task_id))

    def test_observations_are_refused_for_unknown_tickets_and_unwatched_trips(self) -> None:
        task, _ = self._booked_task()
        with self.assertRaisesRegex(WorkflowError, "not part of the booked trip"):
            self.workflow.observe_flight_status(
                task.trip_id,
                _report("NOPE", FlightStatusKind.CANCELLED, at=self.clock.now),
                reported_by="test",
            )
        planned = self.workflow.create_task(make_demo_request(task_id="watch-planned"))
        with self.assertRaisesRegex(WorkflowError, "not being watched"):
            self.workflow.observe_flight_status(
                planned.trip_id,
                _report("MU-EARLY", FlightStatusKind.CANCELLED, at=self.clock.now),
                reported_by="test",
            )

    def test_in_memory_claims_are_exclusive_until_saved(self) -> None:
        task, _ = self._booked_task()
        trips = self.workflow.trips
        due = trips.get(task.trip_id).watch.next_check_at
        first = trips.claim_due_flight_checks(
            worker_id="w1", now=due, lease_duration=timedelta(minutes=5)
        )
        self.assertEqual([t.trip_id for t in first], [task.trip_id])
        self.assertEqual(
            trips.claim_due_flight_checks(
                worker_id="w2", now=due, lease_duration=timedelta(minutes=5)
            ),
            (),
        )
        # 租约过期可以接管；保存也释放。
        self.assertEqual(
            len(
                trips.claim_due_flight_checks(
                    worker_id="w2",
                    now=due + timedelta(minutes=6),
                    lease_duration=timedelta(minutes=5),
                )
            ),
            1,
        )
        trips.save(trips.get(task.trip_id))
        self.assertEqual(
            len(
                trips.claim_due_flight_checks(
                    worker_id="w3", now=due, lease_duration=timedelta(minutes=5)
                )
            ),
            1,
        )
        self.assertEqual([t.trip_id for t in trips.list_watching("MU-EARLY")], [task.trip_id])
        self.assertEqual(trips.list_watching("NOPE"), ())

    def test_sqlalchemy_claims_use_the_projection_and_release_on_save(self) -> None:
        task, _ = self._booked_task()
        trip = self.workflow.trips.get(task.trip_id)
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        repository = SQLAlchemyTripRepository(engine)
        repository.add(trip)
        due = trip.watch.next_check_at
        self.assertEqual(
            repository.claim_due_flight_checks(
                worker_id="w1", now=due - timedelta(minutes=1), lease_duration=timedelta(minutes=5)
            ),
            (),
        )
        claimed = repository.claim_due_flight_checks(
            worker_id="w1", now=due, lease_duration=timedelta(minutes=5)
        )
        self.assertEqual([t.trip_id for t in claimed], [trip.trip_id])
        self.assertEqual(
            repository.claim_due_flight_checks(
                worker_id="w2", now=due, lease_duration=timedelta(minutes=5)
            ),
            (),
        )
        loaded = repository.get(trip.trip_id)
        from dataclasses import replace

        loaded.watch = replace(loaded.watch, next_check_at=due + timedelta(hours=6))
        repository.save(loaded)
        self.assertEqual(
            repository.claim_due_flight_checks(
                worker_id="w2", now=due, lease_duration=timedelta(minutes=5)
            ),
            (),
        )
        self.assertEqual(
            len(
                repository.claim_due_flight_checks(
                    worker_id="w2",
                    now=due + timedelta(hours=6),
                    lease_duration=timedelta(minutes=5),
                )
            ),
            1,
        )
        self.assertEqual(
            [t.trip_id for t in repository.list_watching("MU-EARLY")], [trip.trip_id]
        )
        # 取消之后不再领取。
        cancelled = repository.get(trip.trip_id)
        cancelled.status = TripStatus.CANCELLED
        repository.save(cancelled)
        self.assertEqual(
            repository.claim_due_flight_checks(
                worker_id="w2", now=due + timedelta(days=1), lease_duration=timedelta(minutes=5)
            ),
            (),
        )
        self.assertEqual(repository.list_watching("MU-EARLY"), ())


class CancelTripTests(_Booked):
    def test_cancelling_stops_the_watch_and_refuses_further_changes(self) -> None:
        task, option = self._booked_task()
        trip = self.workflow.cancel_trip(task.trip_id, reported_by="E1001", reason="项目取消了")
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual(trip.events[-1].event_type, TripEventType.TRIP_CANCELLED)
        self.assertEqual(trip.events[-1].note, "项目取消了")
        self.assertIsNone(trip.watch.next_check_at)
        self.assertIn("TRIP_CANCELLED", self._outbox_types())
        self.assertIn("TRIP_CANCELLED", self._events(task.task_id))
        self.assertEqual(self.workflow.tasks.get(task.task_id).state, TaskState.BOOKING_CONFIRMED)
        self.assertEqual(
            whereabouts((trip,), at=option.legs[0].depart_at + timedelta(minutes=10)), ()
        )
        with self.assertRaisesRegex(WorkflowError, "cancelled"):
            self.workflow.report_trip_event(
                task.trip_id,
                event_type=TripEventType.FLIGHT_CHANGED,
                ref_id=option.legs[0].ref_id,
                reported_by="carrier-feed",
            )
        with self.assertRaisesRegex(WorkflowError, "cancelled"):
            self.workflow.observe_flight_status(
                task.trip_id,
                _report(option.legs[0].ref_id, FlightStatusKind.CANCELLED, at=self.clock.now),
                reported_by="test",
            )
        with self.assertRaisesRegex(WorkflowError, "already cancelled"):
            self.workflow.cancel_trip(task.trip_id, reported_by="E1001")
        self.clock.now = option.legs[0].depart_at - timedelta(hours=48)
        self.assertEqual(self.workflow.process_due_flight_checks(), ())

    def test_a_planned_trip_can_be_cancelled_too(self) -> None:
        planned = self.workflow.create_task(make_demo_request(task_id="cancel-planned"))
        trip = self.workflow.cancel_trip(planned.trip_id, reported_by="E1001")
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertIsNone(trip.watch)


class MeetingMovedLegTests(_Booked):
    def test_a_meeting_move_can_target_the_return_leg(self) -> None:
        task, _ = self._booked_task()
        new_return = task.request.return_before + timedelta(hours=3)
        change = self.workflow.report_trip_event(
            task.trip_id,
            event_type=TripEventType.MEETING_MOVED,
            new_arrive_by=new_return,
            leg_index=1,
            reported_by="E1001",
        )
        self.assertEqual(change.request.return_before, new_return)
        self.assertEqual(change.request.arrive_by, task.request.arrive_by)
        self.assertEqual(self.workflow.trips.get(task.trip_id).events[0].leg_index, 1)

    def test_leg_index_out_of_range_is_refused(self) -> None:
        task, _ = self._booked_task()
        with self.assertRaisesRegex(WorkflowError, "out of range"):
            self.workflow.report_trip_event(
                task.trip_id,
                event_type=TripEventType.MEETING_MOVED,
                new_arrive_by=task.request.arrive_by + timedelta(days=1),
                leg_index=5,
                reported_by="E1001",
            )


class ChatChangeTests(_Booked):
    MOVE = "客户把会议改到8月7号上午10点前了，帮我改一下"

    def _with_model(self, script):
        model = ScriptedChangeModel(script)
        self.workflow.tool_calling_language_model = model
        return model

    def test_meeting_moved_in_chat_opens_a_change_task_and_carries_the_message(self) -> None:
        task, _ = self._booked_task()
        model = self._with_model(
            [
                (
                    "request_trip_change",
                    {
                        "kind": "MEETING_MOVED",
                        "leg_index": 0,
                        "new_arrive_by": "2026-08-07T10:00:00+08:00",
                        "date_evidence": "改到8月7号上午10点前",
                        "reason": "客户把会议改到 8 月 7 日",
                    },
                )
            ]
        )
        change = self.workflow.submit_change_message(task.task_id, self.MOVE, reported_by="E1001")

        self.assertTrue(change.is_change_task)
        self.assertEqual(change.parent_task_id, task.task_id)
        self.assertEqual(change.request.arrive_by, datetime(2026, 8, 7, 10, 0, tzinfo=TZ))
        self.assertEqual(change.state, TaskState.WAITING_FOR_USER)
        self.assertEqual([m.content for m in change.messages], [self.MOVE])
        self.assertEqual(self.workflow.trips.get(task.trip_id).status, TripStatus.CHANGE_REQUESTED)
        original = self.workflow.tasks.get(task.task_id)
        self.assertEqual(original.state, TaskState.BOOKING_CONFIRMED)
        self.assertEqual(original.messages[-1].content, self.MOVE)
        events = self._events(task.task_id)
        self.assertIn("CHANGE_MESSAGE_RECEIVED", events)
        self.assertIn("CHANGE_INTENT_RESOLVED", events)
        self.assertIn("CHANGE_MESSAGE_CARRIED", self._events(change.task_id))
        # 模型看到的：只有两个工具、变更模式、已订行程和旅行者原话。
        seen = model.seen[0]
        self.assertEqual(seen["tools"], ("request_trip_change", "ask_traveler"))
        self.assertEqual(seen["context"]["mode"], "trip_change")
        self.assertIn("MU-EARLY", seen["conversation"])
        self.assertIn(self.MOVE, seen["conversation"])
        # 模型调用没有吃掉已订任务的工具预算。
        change_calls = [r for r in original.tool_calls if r.tool_name == "llm.change_intent"]
        self.assertTrue(change_calls)
        self.assertTrue(all(not r.counts_toward_budget for r in change_calls))

    def test_cancel_in_chat_cancels_the_trip_and_replies(self) -> None:
        task, _ = self._booked_task()
        self._with_model([("request_trip_change", {"kind": "CANCEL_TRIP", "reason": "项目黄了"})])
        result = self.workflow.submit_change_message(task.task_id, "这趟不去了，项目黄了")
        self.assertEqual(result.task_id, task.task_id)
        self.assertEqual(result.state, TaskState.BOOKING_CONFIRMED)
        self.assertEqual(result.messages[-1].role, "assistant")
        self.assertIn("取消", result.messages[-1].content)
        self.assertEqual(self.workflow.trips.get(task.trip_id).status, TripStatus.CANCELLED)
        self.assertIn("TRIP_CANCELLED", self._events(task.task_id))

    def test_a_self_reported_flight_change_is_labelled_as_such(self) -> None:
        task, option = self._booked_task()
        self._with_model(
            [
                (
                    "request_trip_change",
                    {
                        "kind": "FLIGHT_CHANGED",
                        "ref_id": option.legs[0].ref_id,
                        "reason": "航司短信说取消了",
                    },
                )
            ]
        )
        change = self.workflow.submit_change_message(task.task_id, "航司短信说我早班机取消了")
        self.assertTrue(change.is_change_task)
        event = self.workflow.trips.get(task.trip_id).events[0]
        self.assertEqual(event.event_type, TripEventType.FLIGHT_CHANGED)
        self.assertTrue(event.note.startswith("旅行者自述："))
        self.assertEqual(event.reported_by, "E1001")

    def test_an_unclear_message_gets_a_question_and_changes_nothing(self) -> None:
        task, _ = self._booked_task()
        self._with_model([("ask_traveler", {"question": "是会议改了时间，还是这趟不去了？"})])
        result = self.workflow.submit_change_message(task.task_id, "情况有变")
        self.assertEqual(result.task_id, task.task_id)
        self.assertEqual(result.messages[-1].content, "是会议改了时间，还是这趟不去了？")
        self.assertEqual(self.workflow.trips.get(task.trip_id).status, TripStatus.BOOKED)
        self.assertIn("CHANGE_INTENT_QUESTION", self._events(task.task_id))

    def test_an_invented_date_is_bounced_back_and_the_model_may_retry(self) -> None:
        task, _ = self._booked_task()
        model = self._with_model(
            [
                (
                    "request_trip_change",
                    {
                        "kind": "MEETING_MOVED",
                        "new_arrive_by": "2026-08-09T10:00:00+08:00",
                        "date_evidence": "改到8月9号",  # 对话里没有这句
                        "reason": "会议改期",
                    },
                ),
                ("ask_traveler", {"question": "会议改到哪一天？"}),
            ]
        )
        result = self.workflow.submit_change_message(task.task_id, "会议改期了，帮我改")
        self.assertEqual(result.messages[-1].content, "会议改到哪一天？")
        # 第二轮模型看见了第一轮被打回的原因。
        bounced = model.seen[1]["transcript"][0]
        self.assertFalse(bounced.ok)
        self.assertEqual(bounced.result["field"], "date_evidence")
        self.assertEqual(self.workflow.trips.get(task.trip_id).status, TripStatus.BOOKED)

    def test_a_quote_that_names_a_different_day_is_bounced(self) -> None:
        task, _ = self._booked_task()
        model = self._with_model(
            [
                (
                    "request_trip_change",
                    {
                        "kind": "MEETING_MOVED",
                        "new_arrive_by": "2026-08-09T10:00:00+08:00",
                        "date_evidence": "改到8月7号上午10点前",
                        "reason": "会议改期",
                    },
                ),
            ]
        )
        result = self.workflow.submit_change_message(task.task_id, self.MOVE)
        self.assertEqual(result.task_id, task.task_id)
        self.assertEqual(len(model.seen), 2)
        self.assertIn("对不上", model.seen[1]["transcript"][0].result["error"])

    def test_change_messages_are_refused_when_there_is_nothing_to_change(self) -> None:
        task, _ = self._booked_task()
        self._with_model([("request_trip_change", {"kind": "CANCEL_TRIP", "reason": "x"})])
        planned = self.workflow.create_task(make_demo_request(task_id="chat-planned"))
        with self.assertRaisesRegex(WorkflowError, "Cannot submit a change message"):
            self.workflow.submit_change_message(planned.task_id, "这趟不去了")
        self.workflow.report_trip_event(
            task.trip_id,
            event_type=TripEventType.MEETING_MOVED,
            new_arrive_by=task.request.arrive_by + timedelta(hours=6),
            reported_by="E1001",
        )
        with self.assertRaisesRegex(WorkflowError, "already in progress"):
            self.workflow.submit_change_message(task.task_id, "这趟不去了")

    def test_without_a_model_the_change_chat_is_unavailable(self) -> None:
        task, _ = self._booked_task()
        self.workflow.tool_calling_language_model = None
        with self.assertRaises(LanguageModelUnavailable):
            self.workflow.submit_change_message(task.task_id, "这趟不去了")


class TripWatchApiTests(unittest.TestCase):
    """`/trips/{id}/flight-status`、`/flight-status/webhook`、`/trips/{id}/cancel`、`/trip-watch/run`。"""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from corporate_travel_agent.api import main as api_main

        self.api_main = api_main
        self.app = api_main.app
        self.client = TestClient(api_main.app)
        self._previous_clock = api_main.workflow.clock
        self._previous_model = api_main.workflow.tool_calling_language_model
        api_main.workflow.clock = lambda: DEMO_CLOCK

    def tearDown(self) -> None:
        self.api_main.workflow.clock = self._previous_clock
        self.api_main.workflow.tool_calling_language_model = self._previous_model
        from corporate_travel_agent.api.main import _current_identity

        self.app.dependency_overrides.pop(_current_identity, None)
        self.api_main.runtime.flight_status_webhook_secret = None

    def _as_employee(self, employee_id: str) -> None:
        from corporate_travel_agent.api.main import _current_identity
        from corporate_travel_agent.services.auth import Role, UserIdentity

        self.app.dependency_overrides[_current_identity] = lambda: UserIdentity(
            user_id=f"user-{employee_id}", roles=frozenset({Role.EMPLOYEE}), employee_id=employee_id
        )

    def _as_admin(self) -> None:
        from corporate_travel_agent.api.main import _current_identity

        self.app.dependency_overrides.pop(_current_identity, None)

    def _booked_via_api(self) -> tuple[str, str, dict]:
        created = self.client.post(
            "/trip-tasks",
            json={
                "traveler_id": "E1001", "origin": "Beijing", "destination": "Shanghai",
                "departure_after": "2026-08-05T05:00:00+08:00",
                "arrive_by": "2026-08-06T10:00:00+08:00",
                "hard_constraints": ["arrive_before_meeting"],
            },
        ).json()
        compliant = next(o for o in created["options"] if o["policy_outcome"] == "COMPLIANT")
        task_id = created["task_id"]
        self.client.post(
            f"/trip-tasks/{task_id}/select-option", json={"option_id": compliant["option_id"]}
        )
        confirmed = self.client.post(
            f"/trip-tasks/{task_id}/booking-confirmation",
            json={"order_references": ["PNR-API"], "total_amount": str(compliant["total_cost"]),
                  "currency": compliant["currency"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        trip = self.client.get(f"/trips/{created['trip_id']}").json()
        return task_id, created["trip_id"], trip

    def test_health_exposes_the_watch_configuration(self) -> None:
        body = self.client.get("/health").json()["trip_watch"]
        self.assertEqual(body["source"], "none")
        self.assertFalse(body["source_configured"])
        self.assertFalse(body["worker_enabled"])
        self.assertEqual(body["lookahead_hours"], 48)
        self.assertIn("metrics", body)

    def test_flight_status_is_assessed_not_blindly_rebooked(self) -> None:
        task_id, trip_id, trip = self._booked_via_api()
        leg = trip["watch"]["legs"][0]
        self._as_employee("E1001")
        forbidden = self.client.post(
            f"/trips/{trip_id}/flight-status", json={"ref_id": leg["ref_id"], "status": "DELAYED"}
        )
        self.assertEqual(forbidden.status_code, 403)
        self._as_admin()
        arrive = datetime.fromisoformat(leg["arrive_at"]) + timedelta(minutes=20)
        delayed = self.client.post(
            f"/trips/{trip_id}/flight-status",
            json={
                "ref_id": leg["ref_id"],
                "status": "DELAYED",
                "estimated_arrive_at": arrive.isoformat(),
                "source": "carrier-feed",
            },
        )
        self.assertEqual(delayed.status_code, 200, delayed.text)
        body = delayed.json()
        self.assertEqual(body["observation"]["verdict"], "NOTIFY_ONLY")
        self.assertIsNone(body["observation"]["opened_task_id"])
        self.assertEqual(body["trip"]["status"], "BOOKED")
        self.assertEqual(len(body["trip"]["watch"]["observations"]), 1)

        cancelled = self.client.post(
            f"/trips/{trip_id}/flight-status",
            json={"ref_id": leg["ref_id"], "status": "CANCELLED"},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        body = cancelled.json()
        self.assertEqual(body["observation"]["verdict"], "REBOOK_REQUIRED")
        self.assertIsNotNone(body["observation"]["opened_task_id"])
        self.assertEqual(body["trip"]["status"], "CHANGE_REQUESTED")
        self.assertEqual(body["trip"]["events"][0]["impact"]["verdict"], "REBOOK_REQUIRED")
        change = self.client.get(f"/trip-tasks/{body['observation']['opened_task_id']}").json()
        self.assertTrue(change["is_change_task"])
        self.assertEqual(change["parent_task_id"], task_id)
        unknown = self.client.post(
            f"/trips/{trip_id}/flight-status", json={"ref_id": "NOPE", "status": "CANCELLED"}
        )
        self.assertEqual(unknown.status_code, 409)

    def test_webhook_requires_a_configured_secret_and_a_valid_signature(self) -> None:
        _, trip_id, trip = self._booked_via_api()
        leg = trip["watch"]["legs"][0]
        payload = json.dumps({"ref_id": leg["ref_id"], "status": "CANCELLED", "source": "tmc"})
        self.assertEqual(
            self.client.post("/flight-status/webhook", content=payload).status_code, 503
        )
        self.api_main.runtime.flight_status_webhook_secret = "s3cret"
        bad = self.client.post(
            "/flight-status/webhook",
            content=payload,
            headers={"X-Flight-Status-Signature": "deadbeef", "Content-Type": "application/json"},
        )
        self.assertEqual(bad.status_code, 401)
        signature = hmac.new(b"s3cret", payload.encode(), hashlib.sha256).hexdigest()
        good = self.client.post(
            "/flight-status/webhook",
            content=payload,
            headers={"X-Flight-Status-Signature": signature, "Content-Type": "application/json"},
        )
        self.assertEqual(good.status_code, 200, good.text)
        body = good.json()
        # 同一班航班可能坐着别的旅行者（同一进程里其它测试订的）：每一趟都算数，我们这趟必须在内。
        self.assertGreaterEqual(body["trip_count"], 1)
        mine = next(item for item in body["results"] if item["trip"]["trip_id"] == trip_id)
        self.assertEqual(mine["trip"]["status"], "CHANGE_REQUESTED")
        self.assertEqual(mine["trip"]["events"][0]["reported_by"], "flight-status:tmc")
        self.assertEqual(mine["observation"]["verdict"], "REBOOK_REQUIRED")
        # 坏 JSON 是 422，不是 500：pydantic 报错里的 input 是 bytes，不能原样塞进 detail。
        broken = "{not json"
        broken_signature = hmac.new(b"s3cret", broken.encode(), hashlib.sha256).hexdigest()
        malformed = self.client.post(
            "/flight-status/webhook",
            content=broken,
            headers={
                "X-Flight-Status-Signature": broken_signature,
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(malformed.status_code, 422, malformed.text)
        # 票号没人盯着：404，不猜。
        orphan = json.dumps({"ref_id": "NOPE", "status": "CANCELLED"})
        orphan_signature = hmac.new(b"s3cret", orphan.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-Flight-Status-Signature": orphan_signature,
            "Content-Type": "application/json",
        }
        self.assertEqual(
            self.client.post("/flight-status/webhook", content=orphan, headers=headers).status_code,
            404,
        )

    def test_the_traveler_can_cancel_and_the_admin_can_run_a_watch_round(self) -> None:
        task_id, trip_id, trip = self._booked_via_api()
        self._as_employee("E1001")
        self.assertEqual(self.client.post("/trip-watch/run").status_code, 403)
        cancelled = self.client.post(f"/trips/{trip_id}/cancel", json={"reason": "不去了"})
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "CANCELLED")
        self.assertEqual(cancelled.json()["events"][-1]["event_type"], "TRIP_CANCELLED")
        again = self.client.post(f"/trips/{trip_id}/cancel", json={})
        self.assertEqual(again.status_code, 409)
        moved = self.client.post(
            f"/trips/{trip_id}/events",
            json={"event_type": "MEETING_MOVED", "new_arrive_by": "2026-08-07T10:00:00+08:00"},
        )
        self.assertEqual(moved.status_code, 409)
        self._as_admin()
        ran = self.client.post("/trip-watch/run")
        self.assertEqual(ran.status_code, 200, ran.text)
        self.assertEqual(ran.json()["source"], "none")
        self.assertEqual(ran.json()["processed"], [])
        care = self.client.get("/duty-of-care").json()
        self.assertNotIn(trip_id, {row["trip_id"] for row in care["travelers"]})

    def test_a_meeting_move_event_can_name_the_leg(self) -> None:
        task_id, trip_id, _ = self._booked_via_api()
        moved = self.client.post(
            f"/trips/{trip_id}/events",
            json={
                "event_type": "MEETING_MOVED",
                "new_arrive_by": "2026-08-07T10:00:00+08:00",
                "leg_index": 0,
            },
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(
            self.client.get(f"/trips/{trip_id}").json()["events"][0]["leg_index"], 0
        )
        out_of_range = self.client.post(
            f"/trips/{trip_id}/events",
            json={"event_type": "MEETING_MOVED", "new_arrive_by": "2026-08-07T10:00:00+08:00",
                  "leg_index": 7},
        )
        self.assertEqual(out_of_range.status_code, 409)

    def test_chat_on_a_booked_task_becomes_a_change_request(self) -> None:
        task_id, trip_id, _ = self._booked_via_api()
        self.api_main.workflow.tool_calling_language_model = ScriptedChangeModel(
            [
                (
                    "request_trip_change",
                    {
                        "kind": "MEETING_MOVED",
                        "leg_index": 0,
                        "new_arrive_by": "2026-08-07T10:00:00+08:00",
                        "date_evidence": "改到8月7号上午10点前",
                        "reason": "会议改期",
                    },
                )
            ]
        )
        self._as_employee("E1001")
        response = self.client.post(
            f"/agentic/trip-tasks/{task_id}/messages",
            json={"message": "客户把会议改到8月7号上午10点前了"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        change = response.json()
        self.assertTrue(change["is_change_task"])
        self.assertEqual(change["parent_task_id"], task_id)
        self.assertEqual(change["change_event"]["event_type"], "MEETING_MOVED")
        self.assertEqual(self.client.get(f"/trips/{trip_id}").json()["status"], "CHANGE_REQUESTED")
        original = self.client.get(f"/trip-tasks/{task_id}").json()
        self.assertEqual(original["state"], "BOOKING_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
