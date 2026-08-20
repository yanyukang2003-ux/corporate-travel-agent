import unittest

from corporate_travel_agent.agent.orchestrator import TripWorkflowOrchestrator
from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, ToolCallStatus


class ToolBudgetTests(unittest.TestCase):
    def test_searches_are_counted_in_one_task_budget(self) -> None:
        workflow, _ = build_demo_system()

        task = workflow.create_task(make_demo_request(task_id="budget-search"))

        self.assertEqual(task.tool_call_limit, 12)
        self.assertEqual(task.tool_calls_used, 3)
        self.assertEqual(task.tool_calls_remaining, 9)
        self.assertEqual(
            [item.tool_name for item in task.tool_calls],
            [
                "provider.search_transport.outbound",
                "provider.search_transport.inbound",
                "provider.search_hotels",
            ],
        )
        self.assertTrue(
            all(item.status is ToolCallStatus.SUCCEEDED for item in task.tool_calls)
        )

    def test_revalidation_and_handoff_are_in_the_same_budget(self) -> None:
        workflow, _ = build_demo_system()
        task = workflow.create_task(make_demo_request(task_id="budget-handoff"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )

        task = workflow.select_option(task.task_id, option.option_id)

        self.assertEqual(task.state, TaskState.READY_FOR_HANDOFF)
        self.assertEqual(task.tool_calls_used, 5)
        self.assertEqual(
            [item.tool_name for item in task.tool_calls[-2:]],
            ["provider.revalidate", "provider.create_deep_link"],
        )

    def test_next_call_is_blocked_at_the_configured_limit(self) -> None:
        workflow, _ = build_demo_system(max_tool_calls=3)
        task = workflow.create_task(make_demo_request(task_id="budget-hard-stop"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )

        task = workflow.select_option(task.task_id, option.option_id)

        self.assertEqual(task.state, TaskState.TOOL_BUDGET_EXHAUSTED)
        self.assertEqual(task.tool_calls_used, 3)
        self.assertEqual(task.tool_calls_remaining, 0)
        self.assertNotIn(
            "provider.revalidate", [item.tool_name for item in task.tool_calls]
        )
        self.assertIsNone(task.booking_intent)
        self.assertIn("remaining 0", task.failure)
        events = [item.event_type for item in workflow.tasks.events(task.task_id)]
        self.assertIn("TOOL_BUDGET_EXHAUSTED", events)

    def test_failed_tool_call_consumes_budget_and_records_failure_type(self) -> None:
        workflow, provider = build_demo_system()
        provider.fail_search = True

        task = workflow.create_task(make_demo_request(task_id="budget-failed-call"))

        self.assertEqual(task.state, TaskState.PROVIDER_FAILED)
        self.assertEqual(task.tool_calls_used, 1)
        self.assertEqual(task.tool_calls[0].status, ToolCallStatus.FAILED)
        self.assertEqual(task.tool_calls[0].error_type, "ProviderError")

    def test_multi_call_search_is_not_started_without_enough_capacity(self) -> None:
        workflow, _ = build_demo_system(max_tool_calls=2)

        task = workflow.create_task(make_demo_request(task_id="budget-preflight"))

        self.assertEqual(task.state, TaskState.TOOL_BUDGET_EXHAUSTED)
        self.assertEqual(task.tool_calls_used, 0)
        self.assertEqual(task.tool_calls_remaining, 2)
        self.assertIn("requires 3", task.failure)

    def test_budget_must_be_positive(self) -> None:
        workflow, provider = build_demo_system()

        with self.assertRaisesRegex(ValueError, "max_tool_calls"):
            TripWorkflowOrchestrator(
                tasks=workflow.tasks,
                employees=workflow.employees,
                policies=workflow.policies,
                provider=provider,
                max_tool_calls=0,
            )


if __name__ == "__main__":
    unittest.main()
