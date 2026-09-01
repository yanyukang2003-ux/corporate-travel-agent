"""事务性发件箱 + 投递：状态变了通知就在，通知发出去了就标已发布，发不出去就计次。

三组：
1. 入队和任务更新是一笔：内存版随 `record()` 写；SQL 版和任务、审计事件同一事务，
   任务更新失败（并发冲突）时发件箱里也不该有它。
2. 投递器：成功标已发布、失败计次、超过次数进死信但不标已发布；按事件类型选通道；
   webhook 通道带签名、非 2xx 就是失败。
3. 端到端：仓库内扮演的"外部审批系统"收到通知、做决定、回调本系统，任务一路走到可交接。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from alembic import command
from alembic.config import Config

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState
from corporate_travel_agent.domain.models import ApprovalTier
from corporate_travel_agent.services.audit import new_audit_event
from corporate_travel_agent.services.outbox import SQLAlchemyOutboxStore
from corporate_travel_agent.services.outbox_dispatch import (
    DeliveryError,
    LoggingChannel,
    OutboxDispatcher,
    SimulatedApprovalSystemChannel,
    WebhookChannel,
)
from corporate_travel_agent.services.outbox_events import (
    InMemoryOutboxStore,
    OutboxEvent,
    OutboxEventDraft,
)
from corporate_travel_agent.services.policy_config import load_policy_configuration
from corporate_travel_agent.services.repositories import ConcurrentUpdateError
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository

NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _event(event_id: str, event_type: str = "APPROVAL_REQUESTED", **payload) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        aggregate_type="trip_task",
        aggregate_id=payload.get("task_id", "t"),
        event_type=event_type,
        payload={
            "task_id": "t",
            "approval_id": "a",
            "step_index": 0,
            "approver_id": "M1",
            **payload,
        },
        created_at=NOW,
    )


class _FailingChannel:
    name = "failing"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def deliver(self, event: OutboxEvent) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise DeliveryError("down")


class TransactionalEnqueueTests(unittest.TestCase):
    def test_the_in_memory_repository_writes_the_outbox_with_the_task(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        task = workflow.create_task(make_demo_request(task_id="mem-outbox"))
        option = next(
            item for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        workflow.select_option(task.task_id, option.option_id, business_reason="客户要求")

        events = workflow.tasks.outbox.list_unpublished(limit=10)
        requested = [item for item in events if item.event_type == "APPROVAL_REQUESTED"]
        self.assertEqual(len(requested), 1)
        payload = requested[0].payload
        self.assertEqual(payload["task_id"], task.task_id)
        self.assertEqual(payload["approver_id"], "M2001")
        self.assertEqual(payload["employee_id"], "E1001")
        self.assertEqual(Decimal(payload["approved_price"]), option.total_cost)
        self.assertTrue(payload["violations"])
        self.assertEqual(requested[0].aggregate_id, task.task_id)

    def test_the_sql_repository_commits_task_audit_and_outbox_together(self) -> None:
        with TemporaryDirectory() as tmp:
            url = f"sqlite+pysqlite:///{Path(tmp) / 'outbox.db'}"
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", url)
            command.upgrade(config, "head")
            repository = SQLAlchemyTaskRepository(url)
            store = SQLAlchemyOutboxStore(repository.engine)
            workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK, task_repository=repository)
            task = workflow.create_task(make_demo_request(task_id="sql-outbox"))
            option = next(
                item for item in task.options
                if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
            )
            workflow.select_option(task.task_id, option.option_id, business_reason="客户要求")

            events = store.list_unpublished(limit=10)
            self.assertEqual([item.event_type for item in events], ["APPROVAL_REQUESTED"])
            self.assertEqual(
                repository.list_task_summaries(pending_approver_id="M2001")[0].task_id,
                task.task_id,
            )

            # 并发冲突：陈旧修订的写入被拒，发件箱里也不能多出一条。
            stale = repository.get(task.task_id)
            stale.persistence_revision -= 1
            with self.assertRaises(ConcurrentUpdateError):
                repository.record(
                    stale,
                    new_audit_event(task.task_id, "STALE", input_value=None, output_value=None),
                    outbox_events=(OutboxEventDraft(event_type="STALE_EVENT", payload={}),),
                )
            self.assertEqual(store.unpublished_count(), 1)
            repository.dispose()


class DispatcherTests(unittest.TestCase):
    def test_delivered_events_are_marked_published(self) -> None:
        store = InMemoryOutboxStore()
        store.add(_event("e1"))
        store.add(_event("e2", "BOOKING_CONFIRMED"))
        channel = LoggingChannel()
        dispatcher = OutboxDispatcher(store, default_channel=channel, clock=lambda: NOW)

        report = dispatcher.dispatch_once()

        self.assertEqual(report.delivered, ("e1", "e2"))
        self.assertEqual([item.event_id for item in channel.delivered], ["e1", "e2"])
        self.assertEqual(store.unpublished_count(), 0)
        self.assertEqual(dispatcher.dispatch_once().delivered, ())

    def test_failures_count_attempts_and_stop_at_the_dead_letter_limit(self) -> None:
        store = InMemoryOutboxStore()
        store.add(_event("e1"))
        channel = _FailingChannel(fail_times=10)
        dispatcher = OutboxDispatcher(store, default_channel=channel, max_attempts=3)

        reports = [dispatcher.dispatch_once() for _ in range(5)]

        self.assertEqual([len(r.failed) for r in reports], [1, 1, 1, 0, 0])
        self.assertEqual([r.dead_lettered for r in reports], [(), (), (), ("e1",), ("e1",)])
        self.assertEqual(channel.calls, 3)
        # 死信不标已发布：它是运维要看见的事实。
        self.assertEqual(store.unpublished_count(), 1)
        self.assertEqual([item.event_id for item in dispatcher.dead_letters()], ["e1"])
        self.assertIn("down", store.list_unpublished()[0].last_error)

    def test_a_transient_failure_recovers_on_a_later_round(self) -> None:
        store = InMemoryOutboxStore()
        store.add(_event("e1"))
        dispatcher = OutboxDispatcher(store, default_channel=_FailingChannel(fail_times=1))

        self.assertEqual(dispatcher.dispatch_once().failed[0][0], "e1")
        self.assertEqual(dispatcher.dispatch_once().delivered, ("e1",))
        self.assertEqual(store.unpublished_count(), 0)

    def test_routes_pick_a_channel_by_event_type(self) -> None:
        store = InMemoryOutboxStore()
        store.add(_event("a", "APPROVAL_REQUESTED"))
        store.add(_event("b", "BOOKING_CONFIRMED"))
        approvals, everything_else = LoggingChannel(), LoggingChannel()
        dispatcher = OutboxDispatcher(
            store, default_channel=everything_else, routes={"APPROVAL_REQUESTED": approvals}
        )

        dispatcher.dispatch_once()

        self.assertEqual([i.event_id for i in approvals.delivered], ["a"])
        self.assertEqual([i.event_id for i in everything_else.delivered], ["b"])


class WebhookChannelTests(unittest.TestCase):
    def _channel(self, handler, secret: str | None = "s3cret") -> WebhookChannel:
        return WebhookChannel(
            "https://oa.example.test/hooks/travel",
            secret=secret,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_posts_the_event_with_an_id_and_a_signature(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            seen["body"] = request.content
            return httpx.Response(202)

        self._channel(handler).deliver(_event("e1", task_id="t-9"))

        headers = seen["headers"]
        body = seen["body"]
        assert isinstance(headers, dict) and isinstance(body, bytes)
        self.assertEqual(headers["x-outbox-event-id"], "e1")
        self.assertEqual(headers["x-outbox-event-type"], "APPROVAL_REQUESTED")
        expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-outbox-signature"], expected)
        self.assertEqual(json.loads(body)["payload"]["task_id"], "t-9")
        self.assertEqual(json.loads(body)["event_id"], "e1")

    def test_a_non_2xx_response_is_a_delivery_failure(self) -> None:
        channel = self._channel(lambda request: httpx.Response(500))
        with self.assertRaises(DeliveryError):
            channel.deliver(_event("e1"))

    def test_a_transport_error_is_a_delivery_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with self.assertRaises(DeliveryError):
            self._channel(handler, secret=None).deliver(_event("e1"))


class SimulatedApprovalSystemTests(unittest.TestCase):
    """仓库内扮演的外部审批系统：端口是真的，对面那个系统是我们自己扮的。"""

    def setUp(self) -> None:
        loaded = load_policy_configuration()
        active = loaded.active_policy
        finance = ApprovalTier(label="finance", approver_id="F3001", above_amount=Decimal("0"))
        two_step = replace(active, approval_tiers=(finance,))
        snapshots = tuple(
            two_step if item.snapshot_id == active.snapshot_id else item
            for item in loaded.policy_snapshots
        )
        self.workflow, _ = build_demo_system(
            clock=lambda: DEMO_CLOCK,
            policy_configuration=replace(loaded, policy_snapshots=snapshots),
        )
        self.oa = SimulatedApprovalSystemChannel(decide=self.workflow.decide_approval)
        self.dispatcher = OutboxDispatcher(self.workflow.tasks.outbox, default_channel=self.oa)

    def test_the_round_trip_walks_both_steps_through_the_external_system(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="oa-roundtrip"))
        option = next(
            item for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        self.workflow.select_option(task.task_id, option.option_id, business_reason="客户要求")

        self.dispatcher.dispatch_once()
        pending = self.oa.pending()
        self.assertEqual([(p.step_label, p.approver_id) for p in pending], [("manager", "M2001")])
        self.assertEqual(pending[0].business_reason, "客户要求")

        # 外部系统里的经理批了：回调本系统，任务走到第二级。
        after_manager = self.oa.decide(task.task_id, approved=True, reason="经理同意")
        self.assertEqual(after_manager.state, TaskState.WAITING_FOR_APPROVAL)
        self.assertEqual(after_manager.approval.approver_id, "F3001")

        self.dispatcher.dispatch_once()
        pending = self.oa.pending()
        self.assertEqual([(p.step_label, p.approver_id) for p in pending], [("finance", "F3001")])

        final = self.oa.decide(task.task_id, approved=True, reason="预算内")
        self.assertEqual(final.state, TaskState.READY_FOR_HANDOFF)
        self.dispatcher.dispatch_once()
        self.assertEqual(self.oa.pending(), ())
        self.assertEqual(self.workflow.tasks.outbox.unpublished_count(), 0)

    def test_redelivery_of_the_same_event_does_not_duplicate_the_todo(self) -> None:
        event = _event("dup", approver_id="M2001", task_id="t-dup")
        self.oa.deliver(event)
        self.oa.deliver(event)
        self.assertEqual(len(self.oa.pending()), 1)


if __name__ == "__main__":
    unittest.main()
