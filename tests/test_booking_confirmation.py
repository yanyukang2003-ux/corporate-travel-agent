"""下单确认回流的测试。

盯三件事：
1. **接线**——真的把演示工作流跑到"可交接"，回填订单号，状态、审计事件、聚合上的记录
   都要对；从 `READY_FOR_HANDOFF` 直接回填要补记一笔交接完成。
2. **边界**——一个任务一条、只能从交接阶段进来、审批人替填之类的越权在 API 层挡
   （见 test_auth）、订单号/金额/币种/时间的形状。
3. **差额只有一个算法**——币种一致算得出来，不一致就是 None，不换算。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import (
    BookingConfirmationSource,
    PolicyOutcome,
    TaskState,
)
from corporate_travel_agent.domain.validation import (
    BookingConfirmationValidationError,
    validate_booking_confirmation_values,
)
from corporate_travel_agent.workflow.state_machine import InvalidTransition, StateMachine


class BookingConfirmationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)

    def _ready_task(self, task_id: str):
        task = self.workflow.create_task(make_demo_request(task_id=task_id))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        task = self.workflow.select_option(task.task_id, option.option_id)
        self.assertEqual(task.state, TaskState.READY_FOR_HANDOFF)
        return task, option

    def _event_types(self, task_id: str) -> list[str]:
        return [item.event_type for item in self.workflow.tasks.events(task_id)]

    def test_confirm_after_handoff_records_the_confirmation(self) -> None:
        task, option = self._ready_task("confirm-after-handoff")
        self.workflow.mark_handed_off(task.task_id)

        task = self.workflow.confirm_booking(
            task.task_id,
            order_references=["  PNR-ABC123 ", "PNR-ABC123", "HTL-9"],
            total_amount=option.total_cost + Decimal("20"),
            currency=option.currency,
            reported_by="E1001",
            note="  酒店多付了 20  ",
        )

        self.assertEqual(task.state, TaskState.BOOKING_CONFIRMED)
        confirmation = task.booking_confirmation
        self.assertIsNotNone(confirmation)
        assert confirmation is not None
        # 去空白、去重，顺序保留。
        self.assertEqual(confirmation.order_references, ("PNR-ABC123", "HTL-9"))
        self.assertEqual(confirmation.option_id, option.option_id)
        self.assertEqual(confirmation.intent_id, task.booking_intent.intent_id)
        self.assertIs(confirmation.source, BookingConfirmationSource.SELF_REPORTED)
        self.assertEqual(confirmation.reported_by, "E1001")
        self.assertEqual(confirmation.reported_at, DEMO_CLOCK)
        # 没说什么时候订的，就当作现在。
        self.assertEqual(confirmation.booked_at, DEMO_CLOCK)
        self.assertEqual(confirmation.note, "酒店多付了 20")
        self.assertEqual(task.booking_cost_variance(), Decimal("20"))

        events = self._event_types(task.task_id)
        self.assertEqual(events.count("HANDOFF_COMPLETED"), 1)
        self.assertEqual(events.count("BOOKING_CONFIRMED"), 1)
        self.assertLess(events.index("HANDOFF_COMPLETED"), events.index("BOOKING_CONFIRMED"))

    def test_confirm_straight_from_ready_for_handoff_records_the_handoff_too(self) -> None:
        """拿着链接去订了、回来直接填单号：交接完成那一笔不能少。"""
        task, option = self._ready_task("confirm-from-ready")

        task = self.workflow.confirm_booking(
            task.task_id,
            order_references=["ORDER-1"],
            total_amount=option.total_cost,
            currency=option.currency,
            reported_by="E1001",
        )

        self.assertEqual(task.state, TaskState.BOOKING_CONFIRMED)
        events = self._event_types(task.task_id)
        self.assertEqual(events.count("HANDOFF_COMPLETED"), 1)
        self.assertEqual(events.count("BOOKING_CONFIRMED"), 1)
        self.assertEqual(task.booking_cost_variance(), Decimal("0"))

    def test_a_task_takes_exactly_one_confirmation(self) -> None:
        task, option = self._ready_task("confirm-once")
        self.workflow.confirm_booking(
            task.task_id,
            order_references=["ORDER-1"],
            total_amount=option.total_cost,
            currency=option.currency,
            reported_by="E1001",
        )

        with self.assertRaises(WorkflowError):
            self.workflow.confirm_booking(
                task.task_id,
                order_references=["ORDER-2"],
                total_amount=option.total_cost,
                currency=option.currency,
                reported_by="E1001",
            )
        self.assertEqual(
            self.workflow.tasks.get(task.task_id).booking_confirmation.order_references,
            ("ORDER-1",),
        )

    def test_cannot_confirm_before_the_handoff_stage(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="confirm-too-early"))
        self.assertEqual(task.state, TaskState.WAITING_FOR_USER)

        with self.assertRaises(WorkflowError):
            self.workflow.confirm_booking(
                task.task_id,
                order_references=["ORDER-1"],
                total_amount=Decimal("100"),
                currency="USD",
                reported_by="E1001",
            )
        self.assertEqual(self.workflow.tasks.get(task.task_id).state, TaskState.WAITING_FOR_USER)
        self.assertNotIn("BOOKING_CONFIRMED", self._event_types(task.task_id))

    def test_validation_failure_leaves_the_task_untouched(self) -> None:
        task, _ = self._ready_task("confirm-invalid")

        with self.assertRaises(WorkflowError):
            self.workflow.confirm_booking(
                task.task_id,
                order_references=["   "],
                total_amount=Decimal("100"),
                currency="USD",
                reported_by="E1001",
            )
        stored = self.workflow.tasks.get(task.task_id)
        self.assertEqual(stored.state, TaskState.READY_FOR_HANDOFF)
        self.assertIsNone(stored.booking_confirmation)
        self.assertNotIn("HANDOFF_COMPLETED", self._event_types(task.task_id))

    def test_currency_mismatch_keeps_the_amount_but_reports_no_variance(self) -> None:
        task, option = self._ready_task("confirm-cny")
        self.assertNotEqual(option.currency, "CNY")

        task = self.workflow.confirm_booking(
            task.task_id,
            order_references=["ORDER-CNY"],
            total_amount=Decimal("8600.50"),
            currency="CNY",
            reported_by="E1001",
        )

        self.assertEqual(task.booking_confirmation.currency, "CNY")
        self.assertEqual(task.booking_confirmation.total_amount, Decimal("8600.50"))
        self.assertIsNone(task.booking_cost_variance())

    def test_booked_at_is_kept_when_supplied(self) -> None:
        task, option = self._ready_task("confirm-booked-at")
        booked_at = DEMO_CLOCK - timedelta(hours=3)

        task = self.workflow.confirm_booking(
            task.task_id,
            order_references=["ORDER-1"],
            total_amount=option.total_cost,
            currency=option.currency,
            reported_by="E1001",
            booked_at=booked_at,
        )

        self.assertEqual(task.booking_confirmation.booked_at, booked_at)
        self.assertEqual(task.booking_confirmation.reported_at, DEMO_CLOCK)

    def test_confirmed_state_is_terminal(self) -> None:
        machine = StateMachine()
        self.assertEqual(
            machine.transition(TaskState.HANDED_OFF, TaskState.BOOKING_CONFIRMED),
            TaskState.BOOKING_CONFIRMED,
        )
        for target in TaskState:
            with self.assertRaises(InvalidTransition):
                machine.transition(TaskState.BOOKING_CONFIRMED, target)


class BookingConfirmationValidationTests(unittest.TestCase):
    """形状校验。每一条都有一个容易写错的默认答案。"""

    def _validate(self, **overrides):
        values = {
            "order_references": ["ORDER-1"],
            "total_amount": Decimal("100"),
            "currency": "USD",
            "booked_at": None,
            "now": DEMO_CLOCK,
            "note": None,
        }
        values.update(overrides)
        return validate_booking_confirmation_values(**values)

    def test_zero_amount_is_allowed(self) -> None:
        """积分票、协议价预付都可能是 0。拒绝 0 会把真实情况挡在门外。"""
        self.assertEqual(self._validate(total_amount=Decimal("0")).total_amount, Decimal("0"))

    def test_negative_or_non_finite_amount_is_rejected(self) -> None:
        for bad in (Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
            with self.assertRaises(BookingConfirmationValidationError):
                self._validate(total_amount=bad)

    def test_references_are_trimmed_deduplicated_and_bounded(self) -> None:
        values = self._validate(order_references=[" A ", "B", "A", "", "  "])
        self.assertEqual(values.order_references, ("A", "B"))
        with self.assertRaises(BookingConfirmationValidationError):
            self._validate(order_references=["", "  "])
        with self.assertRaises(BookingConfirmationValidationError):
            self._validate(order_references=[f"R{i}" for i in range(11)])
        with self.assertRaises(BookingConfirmationValidationError):
            self._validate(order_references=["X" * 65])

    def test_references_with_control_characters_are_rejected(self) -> None:
        """订单号会进审计和报表，一个换行就能把一行表撑坏。"""
        with self.assertRaises(BookingConfirmationValidationError):
            self._validate(order_references=["ABC\nDEF"])

    def test_currency_must_be_a_three_letter_code_but_need_not_match_the_plan(self) -> None:
        self.assertEqual(self._validate(currency=" cny ".upper()).currency, "CNY")
        for bad in ("usd", "US", "USDX", "$"):
            with self.assertRaises(BookingConfirmationValidationError):
                self._validate(currency=bad)

    def test_booked_at_defaults_to_now_and_cannot_be_in_the_future_or_naive(self) -> None:
        self.assertEqual(self._validate().booked_at, DEMO_CLOCK)
        earlier = DEMO_CLOCK - timedelta(days=1)
        self.assertEqual(self._validate(booked_at=earlier).booked_at, earlier)
        with self.assertRaises(BookingConfirmationValidationError):
            self._validate(booked_at=DEMO_CLOCK + timedelta(minutes=1))
        with self.assertRaises(BookingConfirmationValidationError):
            self._validate(booked_at=DEMO_CLOCK.replace(tzinfo=None))

    def test_note_is_trimmed_and_bounded(self) -> None:
        self.assertIsNone(self._validate(note="   ").note)
        self.assertEqual(self._validate(note=" ok ").note, "ok")
        with self.assertRaises(BookingConfirmationValidationError):
            self._validate(note="x" * 501)


if __name__ == "__main__":
    unittest.main()
