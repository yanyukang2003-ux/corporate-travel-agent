"""业务指标层的测试。

第一组是端到端：真的把演示工作流跑到"员工说订好了"，再从仓储里的任务和审计事件
把指标算出来。这一组盯的是**接线对不对**——事件名有没有写错、时间戳读不读得到、
政策证据在不在。手搓的 fixture 测不出这些。

第二组是口径：分母为零、时钟倒流、冻住的时钟、既违规又缺证据的方案。这些情况在
演示数据里不会自然发生，但线上一定会遇到，而且每一个都有一个**容易写错的默认答案**
（把测不出来写成 0、把负数照报、把"判不了"算成超标）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState
from corporate_travel_agent.domain.models import (
    AuditEvent,
    EmployeeProfileSnapshot,
    PolicyDecision,
    RuleEvidence,
    TripTask,
)
from corporate_travel_agent.services.evaluation_business import (
    BUSINESS_PROTOCOL_ID,
    METRIC_LABELS,
    BusinessMetricsError,
    build_business_metrics_report,
    render_business_report_markdown,
    summarize_task,
    write_business_metrics_report,
)


def _event(task_id: str, event_type: str, created_at: datetime) -> AuditEvent:
    return AuditEvent(
        event_id=f"{task_id}-{event_type}-{created_at.isoformat()}",
        task_id=task_id,
        event_type=event_type,
        actor_type="SYSTEM",
        input_hash="",
        output_hash="",
        evidence_refs=(),
        created_at=created_at,
    )


def _bare_task(task_id: str, state: TaskState = TaskState.DRAFT) -> TripTask:
    """一个没有请求、没有方案的最小任务，用来单独试某一个口径。"""
    return TripTask(
        task_id=task_id,
        state=state,
        request=None,
        employee=EmployeeProfileSnapshot(
            snapshot_id="emp-snap-1",
            employee_id="E1001",
            level="L5",
            department="Sales",
            home_city="Beijing",
            manager_id="M2001",
        ),
        policy_snapshot_id="policy-1",
    )


class BusinessMetricsEndToEndTests(unittest.TestCase):
    """跑真链路，不搭假数据。"""

    def setUp(self) -> None:
        self.workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)

    def _report_for(self, *task_ids: str, **kwargs):
        tasks = [self.workflow.tasks.get(item) for item in task_ids]
        events = {item: self.workflow.tasks.events(item) for item in task_ids}
        return build_business_metrics_report(tasks, events, **kwargs)

    def test_completed_handoff_is_measured_end_to_end(self) -> None:
        task = self.workflow.create_task(make_demo_request(task_id="biz-handoff"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(task.task_id, option.option_id)
        self.workflow.mark_handed_off(task.task_id)

        report = self._report_for("biz-handoff")
        record = report.records[0]

        self.assertEqual(report.protocol_id, BUSINESS_PROTOCOL_ID)
        self.assertEqual(record.state, TaskState.HANDED_OFF.value)
        self.assertTrue(record.produced_options)
        self.assertTrue(record.handed_off)
        self.assertTrue(record.has_selected_option)
        # 起点和终点都从真实审计事件里读到了，而不是退化成 None。
        self.assertIsNotNone(record.started_at)
        self.assertIsNotNone(record.handed_off_at)
        self.assertIsNotNone(record.seconds_to_handoff)
        self.assertGreaterEqual(record.seconds_to_handoff, 0.0)
        # 演示工作流的时钟冻在 DEMO_CLOCK，审计时间戳走真实墙上时间，于是"出发"
        # 早于"交接"。这条 note 就是这一层该有的表现：把时间线对不上说出来，
        # 而不是照报一个负的提前天数。传冻住的时钟怎么修，见
        # test_advance_days_uses_the_frozen_clock_when_one_is_supplied。
        self.assertEqual(record.notes, ("departure_before_handoff",))
        self.assertLess(record.advance_days, 0.0)

        self.assertEqual(report.metrics["handoff_completion_rate"].status, "measured")
        self.assertEqual(report.metrics["handoff_completion_rate"].value, 1.0)

    def _confirm(
        self, task_id: str, *, currency: str | None = None, extra: Decimal = Decimal("30")
    ):
        task = self.workflow.create_task(make_demo_request(task_id=task_id))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(task.task_id, option.option_id)
        self.workflow.confirm_booking(
            task.task_id,
            order_references=["PNR-1"],
            total_amount=option.total_cost + extra,
            currency=currency or option.currency,
            reported_by="E1001",
            booked_at=DEMO_CLOCK - timedelta(hours=2),
        )
        return option

    def test_confirmed_booking_is_measured_with_the_reported_amount(self) -> None:
        """回填订单号之后：确认预订率有分子，提前天数起点换成下单时刻，实付偏差算得出来。"""
        option = self._confirm("biz-confirmed")

        report = self._report_for("biz-confirmed")
        record = report.records[0]

        self.assertEqual(record.state, TaskState.BOOKING_CONFIRMED.value)
        # 直接从「可交接」回填，交接完成那一笔也补记了。
        self.assertTrue(record.handed_off)
        self.assertTrue(record.booking_confirmed)
        self.assertEqual(record.booked_at, DEMO_CLOCK - timedelta(hours=2))
        self.assertEqual(record.planned_total, option.total_cost)
        self.assertEqual(record.actual_total, option.total_cost + Decimal("30"))
        self.assertEqual(record.cost_variance, Decimal("30"))
        self.assertEqual(record.cost_variance_currency, option.currency)
        # 起点是员工说的下单时刻，它和出发时刻在同一条（冻住的）时间线上，
        # 所以提前天数是正的，不再需要调用方传时钟来修。
        self.assertEqual(record.advance_days_reference, "booking_confirmation")
        self.assertGreater(record.advance_days, 0.0)
        self.assertNotIn("departure_before_booking", record.notes)
        self.assertNotIn("departure_before_handoff", record.notes)

        metrics = report.metrics
        self.assertEqual(metrics["booking_confirmation_rate"].value, 1.0)
        self.assertEqual(metrics["booking_confirmation_rate.of_handed_off"].value, 1.0)
        self.assertEqual(metrics["booked_cost_variance_ratio_mean"].status, "measured")
        self.assertAlmostEqual(
            metrics["booked_cost_variance_ratio_mean"].value,
            float(Decimal("30") / option.total_cost),
        )

    def test_reported_booking_time_beats_a_supplied_clock(self) -> None:
        """传了冻住的时钟也不替换员工说的下单时刻——那是他说的事实，不是这一刻的读数。"""
        self._confirm("biz-confirmed-clock")

        record = self._report_for(
            "biz-confirmed-clock", handoff_reference_time=DEMO_CLOCK + timedelta(days=3)
        ).records[0]

        self.assertEqual(record.advance_days_reference, "booking_confirmation")
        self.assertEqual(record.booked_at, DEMO_CLOCK - timedelta(hours=2))

    def test_handoff_without_confirmation_shows_the_gap(self) -> None:
        """点了「我去订了」但没回来填单号：交接完成率算他，确认预订率不算他。"""
        handed = self.workflow.create_task(make_demo_request(task_id="biz-gap-handed"))
        option = next(
            item
            for item in handed.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(handed.task_id, option.option_id)
        self.workflow.mark_handed_off(handed.task_id)
        self._confirm("biz-gap-confirmed")

        metrics = self._report_for("biz-gap-handed", "biz-gap-confirmed").metrics

        self.assertEqual(metrics["handoff_completion_rate"].value, 1.0)
        self.assertEqual(metrics["booking_confirmation_rate"].value, 0.5)
        self.assertEqual(metrics["booking_confirmation_rate.of_handed_off"].value, 0.5)

    def test_currency_mismatch_is_flagged_not_converted(self) -> None:
        option = self._confirm("biz-confirmed-cny", currency="CNY", extra=Decimal("7000"))
        self.assertNotEqual(option.currency, "CNY")

        report = self._report_for("biz-confirmed-cny")
        record = report.records[0]

        self.assertTrue(record.booking_confirmed)
        self.assertEqual(record.actual_total, option.total_cost + Decimal("7000"))
        self.assertEqual(record.planned_total, option.total_cost)
        self.assertIsNone(record.cost_variance)
        self.assertIsNone(record.cost_variance_currency)
        self.assertIn("confirmation_currency_mismatch", record.notes)
        self.assertEqual(report.metrics["booked_cost_variance_ratio_mean"].status, "unavailable")
        # 确认本身照算——币种不同影响的是差额，不是「有没有订」。
        self.assertEqual(report.metrics["booking_confirmation_rate"].value, 1.0)

    def test_options_shown_but_never_taken_is_the_metric_this_layer_exists_for(
        self,
    ) -> None:
        """系统给了方案，员工没去订——这正是"绕开去用携程"的可观测形态。"""
        self.workflow.create_task(make_demo_request(task_id="biz-abandoned"))

        report = self._report_for("biz-abandoned")
        record = report.records[0]

        self.assertTrue(record.produced_options)
        self.assertFalse(record.handed_off)
        # 进了分母但没进分子，所以是 0.0——一个测出来的 0，不是"没数据"。
        metric = report.metrics["handoff_completion_rate"]
        self.assertEqual(metric.status, "measured")
        self.assertEqual(metric.value, 0.0)
        self.assertEqual(metric.denominator, 1.0)
        # 没交接就没有耗时和提前天数，而且是 unavailable 不是 0。
        self.assertIsNone(record.seconds_to_handoff)
        self.assertIsNone(record.advance_days)
        self.assertIsNone(record.advance_days_reference)
        self.assertEqual(report.metrics["seconds_to_handoff_mean"].status, "unavailable")

    def test_a_task_that_never_produced_options_stays_out_of_the_main_denominator(
        self,
    ) -> None:
        """系统自己没干成活，不该算进"员工不愿意用"。"""
        self.workflow.create_task(make_demo_request(task_id="biz-shown"))
        blank = _bare_task("biz-blank")
        self.workflow.tasks.add(blank)

        report = self._report_for("biz-shown", "biz-blank")

        self.assertEqual(report.task_count, 2)
        self.assertEqual(report.metrics["handoff_completion_rate"].denominator, 1.0)
        self.assertEqual(
            report.metrics["handoff_completion_rate.all_tasks"].denominator, 2.0
        )

    def test_approval_path_shows_up_as_an_overspend(self) -> None:
        """超标发生率量的是选中方案带不带"需审批"证据。"""
        task = self.workflow.create_task(make_demo_request(task_id="biz-approval"))
        exception_option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        )
        self.workflow.select_option(
            task.task_id,
            exception_option.option_id,
            business_reason="客户会议改期，只剩这班",
        )

        report = self._report_for("biz-approval")
        record = report.records[0]

        self.assertTrue(record.selected_violation_rule_ids)
        self.assertTrue(record.approval_requested)
        selected_rate = report.metrics["policy_violation_rate.selected"]
        self.assertEqual(selected_rate.status, "measured")
        self.assertEqual(selected_rate.value, 1.0)

    def test_advance_days_uses_the_frozen_clock_when_one_is_supplied(self) -> None:
        """离线评测的审计时间戳走真实墙上时间，出发时间在冻住的时间线上。

        不把冻住的时钟传进来，两者相减出来的是一个没有意义的数——这里同时验证
        「传了」和「没传」两条路，并且记下用的是哪一个。
        """
        task = self.workflow.create_task(make_demo_request(task_id="biz-advance"))
        option = next(
            item
            for item in task.options
            if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
        )
        self.workflow.select_option(task.task_id, option.option_id)
        handed = self.workflow.mark_handed_off(task.task_id)
        departure = handed.selected_option().legs[0].depart_at

        frozen = self._report_for("biz-advance", handoff_reference_time=DEMO_CLOCK)
        frozen_record = frozen.records[0]
        self.assertEqual(frozen_record.advance_days_reference, "supplied_clock")
        self.assertAlmostEqual(
            frozen_record.advance_days,
            (departure - DEMO_CLOCK).total_seconds() / 86400.0,
            places=6,
        )

        wall = self._report_for("biz-advance")
        self.assertEqual(wall.records[0].advance_days_reference, "handoff_event")
        self.assertNotAlmostEqual(
            wall.records[0].advance_days, frozen_record.advance_days, places=3
        )


class BusinessMetricsBoundaryTests(unittest.TestCase):
    """口径边界：每一条都有一个容易写错的默认答案。"""

    def test_empty_denominator_is_unavailable_not_zero(self) -> None:
        report = build_business_metrics_report((), {})

        self.assertEqual(report.task_count, 0)
        for name, metric in report.metrics.items():
            with self.subTest(metric=name):
                self.assertEqual(metric.status, "unavailable")
                self.assertIsNone(metric.value)
                self.assertIsNotNone(metric.exposure_note)

    def test_missing_audit_events_do_not_lose_the_aggregate_side_fields(self) -> None:
        """没有审计事件时，时间类指标测不出来，但聚合上读得到的字段照常可用。"""
        task = _bare_task("no-events")
        task.clarification_rounds = 3

        record = summarize_task(task, ())

        self.assertIsNone(record.started_at)
        self.assertIn("no_start_event", record.notes)
        self.assertEqual(record.clarification_rounds, 3)

    def test_handed_off_state_without_the_event_is_flagged_not_guessed(self) -> None:
        task = _bare_task("state-only", state=TaskState.HANDED_OFF)

        record = summarize_task(task, (_event("state-only", "TASK_CREATED", DEMO_CLOCK),))

        self.assertFalse(record.handed_off)
        self.assertIsNone(record.handed_off_at)
        self.assertIn("handed_off_state_without_event", record.notes)

    def test_backwards_clock_reports_nothing_rather_than_a_negative_duration(
        self,
    ) -> None:
        task = _bare_task("backwards", state=TaskState.HANDED_OFF)
        events = (
            _event("backwards", "TASK_CREATED", DEMO_CLOCK),
            _event("backwards", "HANDOFF_COMPLETED", DEMO_CLOCK - timedelta(hours=1)),
        )

        record = summarize_task(task, events)

        self.assertTrue(record.handed_off)
        self.assertIsNone(record.seconds_to_handoff)
        self.assertIn("handoff_before_start", record.notes)

    def test_start_time_falls_back_to_the_earliest_event(self) -> None:
        """四个入口的创建事件都没有时，用最早的一条事件，不要放弃整个指标。"""
        task = _bare_task("fallback", state=TaskState.HANDED_OFF)
        first = DEMO_CLOCK
        events = (
            _event("fallback", "SEARCH_COMMAND_COMPILED", first + timedelta(minutes=5)),
            _event("fallback", "INVENTORY_SNAPSHOT_CAPTURED", first),
            _event("fallback", "HANDOFF_COMPLETED", first + timedelta(minutes=20)),
        )

        record = summarize_task(task, events)

        self.assertEqual(record.started_at, first)
        self.assertEqual(record.seconds_to_handoff, 20 * 60.0)
        self.assertNotIn("no_start_event", record.notes)

    def test_each_entrypoint_creation_event_is_recognised(self) -> None:
        """少认一个入口，那个入口的耗时会静默变成测不出来。"""
        for event_type in (
            "TASK_CREATED",
            "TASK_CREATED_FROM_MESSAGE",
            "SEMANTIC_TASK_CREATED_FROM_MESSAGE",
            "AGENTIC_TASK_CREATED_FROM_MESSAGE",
        ):
            with self.subTest(entrypoint=event_type):
                task = _bare_task("entry")
                events = (
                    # 故意把创建事件排在一条更早的事件之后，逼它按类型选而不是按时间。
                    _event("entry", "TOOL_CALL_STARTED", DEMO_CLOCK - timedelta(hours=1)),
                    _event("entry", event_type, DEMO_CLOCK),
                )
                self.assertEqual(summarize_task(task, events).started_at, DEMO_CLOCK)

    def test_insufficient_evidence_is_not_counted_as_an_overspend(self) -> None:
        """「判不了」是缺数据，不是违规。混进超标发生率会把两件事搅成一个数。"""
        task = _bare_task("evidence")
        task.options = [
            _option_with(
                "opt-unjudged",
                PolicyOutcome.INSUFFICIENT_EVIDENCE,
                (("policy.effective_window", PolicyOutcome.INSUFFICIENT_EVIDENCE),),
            )
        ]
        task.selected_option_id = "opt-unjudged"

        record = summarize_task(task, ())

        self.assertEqual(record.selected_violation_rule_ids, ())
        self.assertEqual(record.offered_options_with_violation, 0)

    def test_a_violation_hidden_behind_insufficient_evidence_is_still_counted(
        self,
    ) -> None:
        """聚合把「证据不足」排在「禁止」之上，只看聚合会漏掉违规。这里逐条读证据。"""
        task = _bare_task("hidden")
        task.options = [
            _option_with(
                "opt-hidden",
                PolicyOutcome.INSUFFICIENT_EVIDENCE,
                (
                    ("policy.effective_window", PolicyOutcome.INSUFFICIENT_EVIDENCE),
                    ("hotel.city.nightly_cap", PolicyOutcome.FORBIDDEN),
                ),
            )
        ]
        task.selected_option_id = "opt-hidden"

        record = summarize_task(task, ())

        self.assertEqual(record.selected_violation_rule_ids, ("hotel.city.nightly_cap",))

    def test_percentiles_and_mean_use_only_measurable_tasks(self) -> None:
        measured = _bare_task("measured", state=TaskState.HANDED_OFF)
        unmeasured = _bare_task("unmeasured")
        events = {
            "measured": (
                _event("measured", "TASK_CREATED", DEMO_CLOCK),
                _event("measured", "HANDOFF_COMPLETED", DEMO_CLOCK + timedelta(minutes=30)),
            ),
            "unmeasured": (_event("unmeasured", "TASK_CREATED", DEMO_CLOCK),),
        }

        report = build_business_metrics_report((measured, unmeasured), events)

        mean = report.metrics["seconds_to_handoff_mean"]
        self.assertEqual(mean.status, "measured")
        self.assertEqual(mean.value, 1800.0)
        # 分母只有 1：测不出来的那个任务没有被当成 0 拉低平均。
        self.assertEqual(mean.denominator, 1.0)
        self.assertEqual(report.metrics["seconds_to_handoff_p50"].value, 1800.0)


class BusinessReportOutputTests(unittest.TestCase):
    """报告落盘：目录约定、给人看的那一份、以及"每个指标都得有口径说明"。"""

    def test_every_metric_has_a_plain_language_label(self) -> None:
        """加了指标没写口径说明，报告里就会出现一个没人看得懂的数。

        这条测试的作用是让"忘了写说明"立刻失败，而不是等到有人读报告时才发现。
        """
        report = build_business_metrics_report((), {})

        missing = sorted(set(report.metrics) - set(METRIC_LABELS))
        self.assertEqual(missing, [], f"这些指标还没写口径说明：{missing}")
        unused = sorted(set(METRIC_LABELS) - set(report.metrics))
        self.assertEqual(unused, [], f"这些说明对应的指标已经不存在了：{unused}")

    def test_report_directory_must_be_new(self) -> None:
        """评测目录是证据，覆盖一次就没有第二份可比。"""
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "run-1"
            report = build_business_metrics_report((), {})
            written = write_business_metrics_report(report, target)

            self.assertTrue((written / "report.json").is_file())
            self.assertTrue((written / "REPORT.md").is_file())
            with self.assertRaises(BusinessMetricsError):
                write_business_metrics_report(report, target)

    def test_markdown_says_unmeasurable_instead_of_zero(self) -> None:
        report = build_business_metrics_report((), {})

        markdown = render_business_report_markdown(report)

        self.assertIn("测不出来", markdown)
        self.assertIn("handoff_completion_rate", markdown)
        # 一个分母为零的指标不能在给人看的那一份里显示成 0.0%。
        self.assertNotIn("| 0.0% |", markdown)

    def test_markdown_lists_why_something_could_not_be_measured(self) -> None:
        task = _bare_task("flagged", state=TaskState.HANDED_OFF)
        events = {
            "flagged": (_event("flagged", "TASK_CREATED", DEMO_CLOCK),),
        }

        markdown = render_business_report_markdown(
            build_business_metrics_report((task,), events)
        )

        self.assertIn("handed_off_state_without_event", markdown)
        self.assertIn("1 个任务", markdown)


def _option_with(
    option_id: str,
    outcome: PolicyOutcome,
    evidence: tuple[tuple[str, PolicyOutcome], ...],
):
    from corporate_travel_agent.domain.models import FeasibilityResult, TravelOptionVersion

    return TravelOptionVersion(
        option_id=option_id,
        version=1,
        trip_request_version=1,
        inventory_snapshot_ids=("snap-1",),
        legs=(),
        stays=(),
        total_cost=Decimal("1000"),
        total_duration_minutes=120,
        feasibility=FeasibilityResult(feasible=True, reasons=()),
        policy_decision=PolicyDecision(
            outcome=outcome,
            evidence=tuple(
                RuleEvidence(
                    rule_id=rule_id,
                    actual="actual",
                    threshold="threshold",
                    policy_version="v1",
                    outcome=rule_outcome,
                    message="",
                    exception_allowed=False,
                )
                for rule_id, rule_outcome in evidence
            ),
        ),
        preference_penalty=Decimal("0"),
        score=Decimal("0"),
        explanation_facts=(),
    )


if __name__ == "__main__":
    unittest.main()
