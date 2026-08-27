import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    ApprovalStatus,
    BookingScope,
    PolicyOutcome,
    TaskState,
)
from corporate_travel_agent.workflow.state_machine import InvalidTransition, StateMachine


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow, self.provider = build_demo_system()

    def test_planner_filters_late_train_and_returns_verified_options(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="trip-plan"))

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        self.assertGreaterEqual(len(task.options), 2)
        self.assertTrue(all(option.feasibility.feasible for option in task.options))
        self.assertTrue(all("G-LATE" not in option.inventory_refs for option in task.options))
        self.assertTrue(
            all("G-BUFFER-FAIL" not in option.inventory_refs for option in task.options)
        )
        self.assertTrue(all(option.inventory_snapshot_ids for option in task.options))

    def test_return_only_search_uses_the_declared_leg_without_reversing_again(self) -> None:
        request = replace(
            make_demo_request(task_id="trip-return-only"),
            booking_scope=BookingScope.RETURN_ONLY,
            return_after=None,
            return_before=None,
            hotel_check_in=None,
            hotel_check_out=None,
        )

        task = self.workflow.create_task(request)

        self.assertTrue(task.options)
        self.assertTrue(
            all(
                (option.outbound.origin, option.outbound.destination)
                == ("Beijing", "Shanghai")
                for option in task.options
            )
        )
        self.assertTrue(all(option.inbound is None for option in task.options))
        transport_calls = [
            item
            for item in task.tool_calls
            if item.tool_name.startswith("provider.search_transport")
        ]
        self.assertEqual(len(transport_calls), 1)

    def test_compliant_selection_is_revalidated_before_handoff(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="trip-compliant"))
        option = next(
            item for item in task.options if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )

        task = self.workflow.select_option(task.task_id, option.option_id)

        self.assertEqual(task.state, TaskState.READY_FOR_HANDOFF)
        self.assertIsNotNone(task.booking_intent)
        events = [item.event_type for item in self.workflow.tasks.events(task.task_id)]
        self.assertLess(
            events.index("INVENTORY_REVALIDATED"),
            events.index("BOOKING_INTENT_CREATED"),
        )

    def test_exception_requires_reason_and_assigned_approver(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="trip-approval"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )

        with self.assertRaises(WorkflowError):
            self.workflow.select_option(task.task_id, option.option_id)
        task = self.workflow.select_option(
            task.task_id, option.option_id, business_reason="Customer agenda requires this option"
        )
        self.assertEqual(task.state, TaskState.WAITING_FOR_APPROVAL)

        with self.assertRaises(WorkflowError):
            self.workflow.decide_approval(
                task.task_id, approver_id="NOT-MANAGER", approved=True, reason="ok"
            )

    def test_approved_option_with_price_change_requires_reconfirmation(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="trip-price-change"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        task = self.workflow.select_option(
            task.task_id, option.option_id, business_reason="Nearest hotel is needed"
        )
        self.provider.price_overrides[option.outbound.ref_id] = (
            option.outbound.price + Decimal("100")
        )

        task = self.workflow.decide_approval(
            task.task_id, approver_id="M2001", approved=True, reason="Approved for client visit"
        )

        self.assertEqual(task.state, TaskState.RECONFIRMATION_REQUIRED)
        self.assertIsNone(task.booking_intent)

    def test_request_revision_invalidates_old_approval(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="trip-revision"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        task = self.workflow.select_option(
            task.task_id, option.option_id, business_reason="Business need"
        )
        old_approval = task.approval
        revised = replace(make_demo_request(task_id=task.task_id, version=2), soft_preferences=())

        task = self.workflow.revise_request(task.task_id, revised)

        self.assertEqual(old_approval.status, ApprovalStatus.INVALIDATED)
        self.assertEqual(task.request.version, 2)
        self.assertIsNone(task.approval)

    def test_provider_failure_is_not_reported_as_empty_inventory(self) -> None:
        self.provider.fail_search = True

        task = self.workflow.create_task(make_demo_request(task_id="trip-provider-failure"))

        self.assertEqual(task.state, TaskState.PROVIDER_FAILED)
        self.assertIn("timed out", task.failure)

    def test_structured_chinese_city_aliases_are_canonicalized(self) -> None:
        request = replace(
            make_demo_request(task_id="trip-chinese-cities"),
            origin="北京",
            destination="上海",
        )

        task = self.workflow.create_task(request)

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        self.assertEqual(task.request.origin, "Beijing")
        self.assertEqual(task.request.destination, "Shanghai")

    def test_no_feasible_option_reports_which_inventory_is_missing(self) -> None:
        request = replace(
            make_demo_request(task_id="trip-no-inventory"),
            destination="Guangzhou",
            return_after=None,
            return_before=None,
            hotel_check_in=None,
            hotel_check_out=None,
        )

        task = self.workflow.create_task(request)

        self.assertEqual(task.state, TaskState.NO_FEASIBLE_OPTION)
        self.assertEqual(
            task.metadata["no_feasible_reasons"],
            ("no outbound inventory matched the requested route and time window",),
        )
        self.assertEqual(task.failure, task.metadata["no_feasible_reasons"][0])

    def test_state_machine_rejects_approval_bypass(self) -> None:
        with self.assertRaises(InvalidTransition):
            StateMachine().transition(TaskState.WAITING_FOR_APPROVAL, TaskState.READY_FOR_HANDOFF)

    def test_handoff_expiry_uses_the_workflow_clock(self) -> None:
        fixed_now = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)
        workflow, _ = build_demo_system(clock=lambda: fixed_now)
        task = workflow.create_task(make_demo_request(task_id="trip-current-handoff"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )

        task = workflow.select_option(task.task_id, option.option_id)

        self.assertEqual(task.state, TaskState.READY_FOR_HANDOFF)
        self.assertEqual(
            task.booking_intent.handoff.expires_at,
            fixed_now + timedelta(minutes=10),
        )

    def test_expired_inventory_snapshot_is_rejected(self) -> None:
        fixed_now = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)
        workflow, provider = build_demo_system(clock=lambda: fixed_now)
        original_search = provider.search_transport

        def expired_search(query):
            return replace(
                original_search(query),
                valid_until=fixed_now - timedelta(minutes=1),
            )

        provider.search_transport = expired_search

        task = workflow.create_task(make_demo_request(task_id="trip-expired-snapshot"))

        self.assertEqual(task.state, TaskState.PROVIDER_FAILED)
        self.assertIn("expired or invalid inventory", task.failure)
        self.assertFalse(task.options)

    def test_expired_provider_handoff_is_rejected(self) -> None:
        fixed_now = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)
        workflow, provider = build_demo_system(clock=lambda: fixed_now)
        task = workflow.create_task(make_demo_request(task_id="trip-expired-handoff"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        original_handoff = provider.create_deep_link

        def expired_handoff(selected_option):
            return replace(original_handoff(selected_option), expires_at=fixed_now)

        provider.create_deep_link = expired_handoff

        task = workflow.select_option(task.task_id, option.option_id)

        self.assertEqual(task.state, TaskState.PROVIDER_FAILED)
        self.assertIn("expired or invalid handoff", task.failure)
        self.assertIsNone(task.booking_intent)


if __name__ == "__main__":
    unittest.main()
