"""分级审批：直属经理永远第一级，之后按政策的分级条件追加；每一级各批各的。

审批链是政策的一部分（`approval_tiers`），不是硬编码的"经理然后财务"。
收件箱按"当前待谁批"查：走到第二级时，经理的收件箱里它就没了，财务的才有。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from corporate_travel_agent.agent.orchestrator import TripWorkflowOrchestrator, WorkflowError
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import ApprovalStatus, PolicyOutcome, TaskState
from corporate_travel_agent.domain.models import (
    ApprovalTier,
    EmployeeProfileSnapshot,
    PolicySnapshot,
)
from corporate_travel_agent.services.policy_config import load_policy_configuration
from corporate_travel_agent.services.task_projections import pending_approver_id


def _employee() -> EmployeeProfileSnapshot:
    return EmployeeProfileSnapshot(
        snapshot_id="emp",
        employee_id="E1",
        level="L3",
        department="Sales",
        home_city="Beijing",
        manager_id="M1",
    )


def _policy(*tiers: ApprovalTier) -> PolicySnapshot:
    base = load_policy_configuration().active_policy
    return replace(base, approval_tiers=tuple(tiers))


class _Task:
    """`_approval_steps` 只读 `task.employee`；不用为它建整个任务。"""

    def __init__(self) -> None:
        self.employee = _employee()


class StepRoutingTests(unittest.TestCase):
    def _steps(self, policy: PolicySnapshot, price: str, violations: tuple[str, ...] = ()):
        return TripWorkflowOrchestrator._approval_steps(_Task(), policy, Decimal(price), violations)

    def test_without_tiers_only_the_manager_approves(self) -> None:
        steps = self._steps(_policy(), "9999")
        self.assertEqual([(s.approver_id, s.label) for s in steps], [("M1", "manager")])

    def test_a_price_strictly_above_the_tier_adds_the_tier_approver(self) -> None:
        policy = _policy(
            ApprovalTier(label="finance", approver_id="F1", above_amount=Decimal("2000"))
        )
        self.assertEqual([s.approver_id for s in self._steps(policy, "2000")], ["M1"])
        self.assertEqual([s.approver_id for s in self._steps(policy, "2000.01")], ["M1", "F1"])

    def test_a_rule_can_trigger_a_tier_even_when_the_price_is_small(self) -> None:
        policy = _policy(
            ApprovalTier(
                label="finance",
                approver_id="F1",
                when_rules=frozenset({"budget.cost_center.remaining"}),
            )
        )
        self.assertEqual([s.approver_id for s in self._steps(policy, "10")], ["M1"])
        self.assertEqual(
            [s.approver_id for s in self._steps(policy, "10", ("budget.cost_center.remaining",))],
            ["M1", "F1"],
        )
        # "判不了"的规则带前缀，去掉前缀再比：预算判不了也该让财务看一眼。
        self.assertEqual(
            [
                s.approver_id
                for s in self._steps(policy, "10", ("unjudged:budget.cost_center.remaining",))
            ],
            ["M1", "F1"],
        )

    def test_the_same_person_never_appears_twice(self) -> None:
        policy = _policy(
            ApprovalTier(label="manager-again", approver_id="M1", above_amount=Decimal("0")),
            ApprovalTier(label="finance", approver_id="F1", above_amount=Decimal("0")),
            ApprovalTier(label="finance-again", approver_id="F1", above_amount=Decimal("0")),
        )
        self.assertEqual([s.approver_id for s in self._steps(policy, "1")], ["M1", "F1"])

    def test_tiers_keep_their_configured_order(self) -> None:
        policy = _policy(
            ApprovalTier(label="director", approver_id="D1", above_amount=Decimal("0")),
            ApprovalTier(label="finance", approver_id="F1", above_amount=Decimal("0")),
        )
        self.assertEqual(
            [s.label for s in self._steps(policy, "1")], ["manager", "director", "finance"]
        )


class TwoStepFlowTests(unittest.TestCase):
    """演示系统 + 一条"金额 0 以上都要财务"的分级：每张例外单都走两级。"""

    def setUp(self) -> None:
        loaded = load_policy_configuration()
        active = loaded.active_policy
        finance = ApprovalTier(label="finance", approver_id="F3001", above_amount=Decimal("0"))
        finance_everything = replace(active, approval_tiers=(finance,))
        snapshots = tuple(
            finance_everything if item.snapshot_id == active.snapshot_id else item
            for item in loaded.policy_snapshots
        )
        self.clock_now = DEMO_CLOCK
        self.workflow, self.provider = build_demo_system(
            clock=lambda: self.clock_now,
            policy_configuration=replace(loaded, policy_snapshots=snapshots),
        )

    def _waiting_task(self, task_id: str):
        task = self.workflow.create_task(make_demo_request(task_id=task_id))
        option = next(
            item for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        return self.workflow.select_option(
            task.task_id, option.option_id, business_reason="客户改期"
        )

    def _inbox(self, approver_id: str) -> set[str]:
        return {
            item.task_id
            for item in self.workflow.tasks.list_task_summaries(
                pending_approver_id=approver_id, state="WAITING_FOR_APPROVAL"
            )
        }

    def test_the_chain_is_manager_then_finance_and_the_inbox_follows(self) -> None:
        task = self._waiting_task("two-step")
        approval = task.approval
        assert approval is not None

        self.assertEqual([s.label for s in approval.steps], ["manager", "finance"])
        self.assertEqual(approval.approver_id, "M2001")
        self.assertEqual(pending_approver_id(task), "M2001")
        self.assertIn(task.task_id, self._inbox("M2001"))
        self.assertNotIn(task.task_id, self._inbox("F3001"))

        # 财务不能越过经理先批。
        with self.assertRaises(WorkflowError):
            self.workflow.decide_approval(
                task.task_id,
                approver_id="F3001",
                approved=True,
                reason="ok",
            )

        task = self.workflow.decide_approval(
            task.task_id, approver_id="M2001", approved=True, reason="经理同意"
        )
        self.assertEqual(task.state, TaskState.WAITING_FOR_APPROVAL)
        self.assertEqual(task.approval.status, ApprovalStatus.PENDING)
        self.assertEqual(task.approval.approver_id, "F3001")
        self.assertEqual(task.approval.steps[0].status, ApprovalStatus.APPROVED)
        self.assertEqual(task.approval.steps[0].reason, "经理同意")
        self.assertEqual(task.approval.current_step, 1)
        self.assertNotIn(task.task_id, self._inbox("M2001"))
        self.assertIn(task.task_id, self._inbox("F3001"))

        # 经理不能替财务再批一次。
        with self.assertRaises(WorkflowError):
            self.workflow.decide_approval(
                task.task_id,
                approver_id="M2001",
                approved=True,
                reason="再批",
            )

        task = self.workflow.decide_approval(
            task.task_id, approver_id="F3001", approved=True, reason="预算内"
        )
        self.assertEqual(task.state, TaskState.READY_FOR_HANDOFF)
        self.assertEqual(task.approval.status, ApprovalStatus.APPROVED)
        self.assertEqual([s.status for s in task.approval.steps], [ApprovalStatus.APPROVED] * 2)
        events = [item.event_type for item in self.workflow.tasks.events(task.task_id)]
        self.assertIn("APPROVAL_STEP_ADVANCED", events)
        self.assertEqual(events.count("APPROVAL_DECIDED"), 1)

    def test_a_rejection_at_the_second_step_ends_the_request(self) -> None:
        task = self._waiting_task("reject-second")
        self.workflow.decide_approval(task.task_id, approver_id="M2001", approved=True, reason="ok")

        task = self.workflow.decide_approval(
            task.task_id, approver_id="F3001", approved=False, reason="预算已用完"
        )

        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)
        self.assertEqual(task.approval.status, ApprovalStatus.REJECTED)
        self.assertEqual(task.approval.steps[1].status, ApprovalStatus.REJECTED)
        self.assertEqual(task.approval.decision_reason, "预算已用完")
        self.assertIsNone(task.selected_option_id)

    def test_expiry_between_steps_invalidates_the_whole_request(self) -> None:
        task = self._waiting_task("expire-second")
        self.workflow.decide_approval(task.task_id, approver_id="M2001", approved=True, reason="ok")
        self.clock_now = DEMO_CLOCK + timedelta(hours=25)

        with self.assertRaises(WorkflowError):
            self.workflow.decide_approval(
                task.task_id,
                approver_id="F3001",
                approved=True,
                reason="late",
            )

        stored = self.workflow.tasks.get(task.task_id)
        self.assertEqual(stored.approval.status, ApprovalStatus.INVALIDATED)
        self.assertEqual(stored.state, TaskState.WAITING_FOR_USER)
        self.assertNotIn(task.task_id, self._inbox("F3001"))

    def test_every_step_change_goes_to_the_outbox_with_the_task(self) -> None:
        task = self._waiting_task("outbox-steps")
        self.workflow.decide_approval(task.task_id, approver_id="M2001", approved=True, reason="ok")
        self.workflow.decide_approval(task.task_id, approver_id="F3001", approved=True, reason="ok")

        events = [
            (item.event_type, item.payload["approver_id"], item.payload["step_index"])
            for item in self.workflow.tasks.outbox.list_unpublished(limit=100)
            if item.aggregate_id == task.task_id and item.event_type.startswith("APPROVAL")
        ]
        self.assertEqual(
            events,
            [
                ("APPROVAL_REQUESTED", "M2001", 0),
                ("APPROVAL_STEP_REQUESTED", "F3001", 1),
                ("APPROVAL_DECIDED", "F3001", 1),
            ],
        )


class DemoConfigurationTests(unittest.TestCase):
    """演示政策 v2 的分级：超过 5,000 美元、或预算规则出问题，就多一级财务。"""

    def test_the_demo_policy_routes_large_exceptions_to_finance(self) -> None:
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        task = workflow.create_task(make_demo_request(task_id="demo-tiers"))
        option = next(
            item for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        task = workflow.select_option(task.task_id, option.option_id, business_reason="客户要求")

        steps = [s.approver_id for s in task.approval.steps]
        if option.total_cost > Decimal("5000"):
            self.assertEqual(steps, ["M2001", "F3001"])
        else:
            self.assertEqual(steps, ["M2001"])

    def test_a_budget_overrun_always_reaches_finance_in_the_demo(self) -> None:
        """预算超支不看金额大小：财务永远要看一眼——演示配置里 when_rules 就是这么写的。"""
        workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)
        first = workflow.create_task(make_demo_request(task_id="demo-budget-first"))
        compliant = next(
            item for item in first.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        workflow.select_option(first.task_id, compliant.option_id)
        workflow.confirm_booking(
            first.task_id,
            order_references=["PNR-BIG"],
            total_amount=Decimal("19000"),
            currency=compliant.currency,
            reported_by="E1001",
        )
        second = workflow.create_task(make_demo_request(task_id="demo-budget-second"))
        over_budget = next(
            item for item in second.options
            if "budget.cost_center.remaining" in item.policy_decision.violation_ids
        )
        task = workflow.select_option(
            second.task_id, over_budget.option_id, business_reason="必须去"
        )

        self.assertEqual([s.label for s in task.approval.steps], ["manager", "finance"])


if __name__ == "__main__":
    unittest.main()
