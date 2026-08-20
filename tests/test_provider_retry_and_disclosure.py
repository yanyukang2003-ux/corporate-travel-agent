"""Regression tests for the two Stage-5 bad cases.

D6 recorded both: transient provider faults ended the task instead of being
retried, and provider-declared partial coverage never reached the user. Each
test below pins one of those behaviours plus its safety boundary.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

from corporate_travel_agent.agent.orchestrator import (
    PARTIAL_COVERAGE_METADATA_KEY,
    TRANSIENT_RETRY_REASON,
)
from corporate_travel_agent.agent.ports import WorkflowTraceEvent
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, ToolCallStatus
from corporate_travel_agent.domain.models import InventorySnapshot
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
)
from corporate_travel_agent.services.provider_resilience import ProviderRetryScheduler


class _FaultyTransportProvider:
    """Delegates to the mock provider, failing outbound search on demand."""

    def __init__(self, base, error: Exception, *, fail_times: int) -> None:
        self.base = base
        self.error = error
        self.fail_times = fail_times
        self.outbound_calls = 0

    name = "faulty-transport-mock"

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        self.outbound_calls += 1
        if self.outbound_calls <= self.fail_times:
            raise self.error
        return self.base.search_transport(query)

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        return self.base.search_hotels(query)

    def revalidate(self, refs):
        return self.base.revalidate(refs)

    def create_deep_link(self, option):
        return self.base.create_deep_link(option)


class _PartialHotelProvider:
    """Returns a hotel snapshot the provider itself labels incomplete."""

    name = "partial-hotel-mock"

    def __init__(self, base) -> None:
        self.base = base

    def search_transport(self, query: TransportSearchQuery) -> InventorySnapshot:
        return self.base.search_transport(query)

    def search_hotels(self, query: HotelSearchQuery) -> InventorySnapshot:
        snapshot = self.base.search_hotels(query)
        return replace(
            snapshot,
            items=snapshot.items[:1],
            provider_warnings=(*snapshot.provider_warnings, "partial hotel coverage"),
        )

    def revalidate(self, refs):
        return self.base.revalidate(refs)

    def create_deep_link(self, option):
        return self.base.create_deep_link(option)


class _RecordingObserver:
    def __init__(self) -> None:
        self.events: list[WorkflowTraceEvent] = []

    def record(self, event: WorkflowTraceEvent) -> None:
        self.events.append(event)


class _BlockingRetryProcessor:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def process_due_provider_retries(self, *, limit: int = 20) -> tuple[str, ...]:
        del limit
        self.started.set()
        self.release.wait(timeout=2)
        return ()


def _workflow_with(provider_factory, **kwargs):
    workflow, mock = build_demo_system(**kwargs)
    workflow.provider = provider_factory(mock)
    return workflow, mock


class BoundedProviderRetryTests(unittest.TestCase):
    def test_scheduler_stop_reports_whether_inflight_work_drained(self) -> None:
        processor = _BlockingRetryProcessor()
        scheduler = ProviderRetryScheduler(processor, poll_seconds=0.01)
        scheduler.start()
        self.assertTrue(processor.started.wait(timeout=1))

        self.assertFalse(scheduler.stop(timeout_seconds=0.001))
        processor.release.set()
        self.assertTrue(scheduler.stop(timeout_seconds=1))

    def test_default_policy_can_recover_after_two_transient_faults(self) -> None:
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, RetryableProviderError("transient upstream blip"), fail_times=2
            ),
            retry_sleep=lambda _: None,
        )

        self.assertEqual(workflow.max_provider_attempts, 3)
        task = workflow.create_task(make_demo_request(task_id="retry-recovers-on-third"))

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        outbound = [
            item
            for item in task.tool_calls
            if item.tool_name == "provider.search_transport.outbound"
        ]
        self.assertEqual(len(outbound), 3)
        self.assertEqual(
            [item.status for item in outbound],
            [ToolCallStatus.FAILED, ToolCallStatus.FAILED, ToolCallStatus.SUCCEEDED],
        )

    def test_transient_fault_is_retried_once_and_the_task_completes(self) -> None:
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, RetryableProviderError("transient upstream blip"), fail_times=1
            )
        )

        task = workflow.create_task(make_demo_request(task_id="retry-recovers"))

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        self.assertTrue(task.options)
        # Three planned calls plus exactly one retry of the outbound search.
        self.assertEqual(task.tool_calls_used, 4)
        outbound = [
            item
            for item in task.tool_calls
            if item.tool_name == "provider.search_transport.outbound"
        ]
        self.assertEqual(len(outbound), 2)
        self.assertEqual(outbound[0].status, ToolCallStatus.FAILED)
        self.assertEqual(outbound[0].error_type, "RetryableProviderError")
        self.assertEqual(outbound[1].status, ToolCallStatus.SUCCEEDED)

    def test_immediate_retry_is_bounded_then_waits_for_provider(self) -> None:
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, RetryableProviderError("upstream is down"), fail_times=99
            ),
            retry_sleep=lambda _: None,
        )

        task = workflow.create_task(make_demo_request(task_id="retry-bounded"))

        self.assertEqual(task.state, TaskState.WAITING_FOR_PROVIDER)
        self.assertEqual(task.tool_calls_used, 3)
        self.assertIsNone(task.booking_intent)
        retry = workflow.provider_retry_status(task)
        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry["status"], "scheduled")
        self.assertEqual(retry["delayed_attempts_completed"], 0)
        self.assertEqual(retry["max_delayed_attempts"], 3)

    def test_first_delayed_retry_recovers_the_task(self) -> None:
        now = [datetime(2026, 8, 10, tzinfo=UTC)]
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, RetryableProviderError("upstream is down"), fail_times=3
            ),
            clock=lambda: now[0],
            retry_sleep=lambda _: None,
            delayed_provider_retry_seconds=(60.0, 60.0, 60.0),
        )
        task = workflow.create_task(make_demo_request(task_id="delayed-recovers"))
        self.assertEqual(task.state, TaskState.WAITING_FOR_PROVIDER)

        now[0] += timedelta(seconds=60)
        self.assertEqual(workflow.process_due_provider_retries(), (task.task_id,))

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        retry = workflow.provider_retry_status(task)
        assert retry is not None
        self.assertEqual(retry["status"], "recovered")
        self.assertEqual(retry["delayed_attempts_completed"], 1)

    def test_three_delayed_retries_exhaust_to_terminal_failure(self) -> None:
        now = [datetime(2026, 8, 10, tzinfo=UTC)]
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, RetryableProviderError("upstream is down"), fail_times=99
            ),
            clock=lambda: now[0],
            retry_sleep=lambda _: None,
            max_tool_calls=20,
            delayed_provider_retry_seconds=(60.0, 60.0, 60.0),
        )
        task = workflow.create_task(make_demo_request(task_id="delayed-exhausted"))

        for _ in range(3):
            now[0] += timedelta(seconds=60)
            self.assertEqual(workflow.process_due_provider_retries(), (task.task_id,))

        self.assertEqual(task.state, TaskState.PROVIDER_FAILED)
        retry = workflow.provider_retry_status(task)
        assert retry is not None
        self.assertEqual(retry["status"], "exhausted")
        self.assertEqual(retry["delayed_attempts_completed"], 3)
        self.assertIsNone(retry["next_retry_at"])
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        self.assertEqual(events.count("PROVIDER_DELAYED_RETRY_STARTED"), 3)
        self.assertIn("PROVIDER_DELAYED_RETRIES_EXHAUSTED", events)

        provider = workflow.provider
        provider.fail_times = provider.outbound_calls
        now[0] += timedelta(seconds=60)
        task = workflow.retry_or_replan(task.task_id)

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        self.assertIsNone(workflow.provider_retry_status(task))

    def test_open_circuit_schedules_new_task_without_calling_provider(self) -> None:
        now = [datetime(2026, 8, 10, tzinfo=UTC)]
        workflow, mock = build_demo_system(
            clock=lambda: now[0],
            retry_sleep=lambda _: None,
        )
        provider = _FaultyTransportProvider(
            mock, RetryableProviderError("upstream is down"), fail_times=99
        )
        workflow.provider = provider

        first = workflow.create_task(make_demo_request(task_id="circuit-first"))
        self.assertEqual(first.state, TaskState.WAITING_FOR_PROVIDER)
        self.assertEqual(provider.outbound_calls, 3)

        second = workflow.create_task(make_demo_request(task_id="circuit-second"))

        self.assertEqual(second.state, TaskState.WAITING_FOR_PROVIDER)
        self.assertEqual(second.tool_calls_used, 0)
        self.assertEqual(provider.outbound_calls, 3)

    def test_delayed_retry_resumes_revalidation_without_duplicate_booking_intent(self) -> None:
        now = [datetime(2026, 8, 1, tzinfo=UTC)]
        workflow, provider = build_demo_system(
            clock=lambda: now[0],
            retry_sleep=lambda _: None,
            delayed_provider_retry_seconds=(60.0, 60.0, 60.0),
        )
        task = workflow.create_task(make_demo_request(task_id="delayed-revalidation"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        original_revalidate = provider.revalidate
        failures_remaining = [3]

        def transient_revalidation(refs):
            if failures_remaining[0] > 0:
                failures_remaining[0] -= 1
                raise RetryableProviderError("revalidation transport timeout")
            return original_revalidate(refs)

        provider.revalidate = transient_revalidation
        task = workflow.select_option(task.task_id, option.option_id)
        self.assertEqual(task.state, TaskState.WAITING_FOR_PROVIDER)
        self.assertIsNone(task.booking_intent)

        now[0] += timedelta(seconds=60)
        self.assertEqual(workflow.process_due_provider_retries(), (task.task_id,))

        self.assertEqual(task.state, TaskState.READY_FOR_HANDOFF)
        self.assertIsNotNone(task.booking_intent)
        retry = workflow.provider_retry_status(task)
        assert retry is not None
        self.assertEqual(retry["status"], "recovered")
        self.assertEqual(retry["resume_operation"], "REVALIDATE")

    def test_non_retryable_error_is_never_retried(self) -> None:
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, ProviderError("upstream rejected the request"), fail_times=1
            )
        )

        task = workflow.create_task(make_demo_request(task_id="retry-forbidden"))

        self.assertEqual(task.state, TaskState.PROVIDER_FAILED)
        self.assertEqual(task.tool_calls_used, 1)

    def test_retry_does_not_exceed_the_tool_budget(self) -> None:
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, RetryableProviderError("transient upstream blip"), fail_times=1
            ),
            max_tool_calls=3,
        )

        task = workflow.create_task(make_demo_request(task_id="retry-budget"))

        # The retry consumes the third slot, leaving the inbound leg unaffordable.
        self.assertEqual(task.state, TaskState.TOOL_BUDGET_EXHAUSTED)
        self.assertEqual(task.tool_calls_used, 3)
        self.assertIsNone(task.booking_intent)

    def test_retry_step_carries_retry_of_and_a_reason_code(self) -> None:
        observer = _RecordingObserver()
        workflow, _ = _workflow_with(
            lambda mock: _FaultyTransportProvider(
                mock, RetryableProviderError("transient upstream blip"), fail_times=1
            ),
            trace_observer=observer,
        )

        workflow.create_task(make_demo_request(task_id="retry-trace"))

        outbound = [
            event
            for event in observer.events
            if event.name == "provider.search_transport.outbound"
        ]
        self.assertEqual(len(outbound), 2)
        # The original failure is not itself a retry; the second attempt links back.
        self.assertIsNone(outbound[0].retry_of)
        self.assertIsNone(outbound[0].reason_code)
        self.assertEqual(outbound[1].status, "success")
        self.assertEqual(outbound[1].retry_of, outbound[0].tool_call_sequence)
        self.assertEqual(outbound[1].reason_code, TRANSIENT_RETRY_REASON)


class PartialCoverageDisclosureTests(unittest.TestCase):
    def test_provider_declared_partial_coverage_reaches_the_task(self) -> None:
        workflow, _ = _workflow_with(_PartialHotelProvider)

        task = workflow.create_task(make_demo_request(task_id="partial-disclosed"))

        notices = task.metadata.get(PARTIAL_COVERAGE_METADATA_KEY, ())
        self.assertTrue(notices)
        self.assertTrue(any("partial hotel coverage" in item for item in notices))
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        self.assertIn("PROVIDER_COVERAGE_INCOMPLETE", events)

    def test_complete_coverage_records_no_notice(self) -> None:
        workflow, _ = build_demo_system()

        task = workflow.create_task(make_demo_request(task_id="partial-absent"))

        self.assertEqual(task.metadata.get(PARTIAL_COVERAGE_METADATA_KEY, ()), ())


if __name__ == "__main__":
    unittest.main()
