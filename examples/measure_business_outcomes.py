"""量业务结果：用了多久、提前多少天订、多少单超标、员工有没有真去下单。

和其他 runner 的区别：**它不跑任何用例，也不调模型和供应商**。它读的是已经发生过的
任务和它们的审计事件，所以同一份代码既能算线上真实任务，也能算离线评测跑出来的任务。

用法：

    # 从 PostgreSQL 里已有的任务算
    python examples/measure_business_outcomes.py \\
        --database-url "$DATABASE_URL" \\
        --output reports/evaluation-runs/business-20260831/

    # 没有数据库时，先跑一小段演示流程再算，用来看报告长什么样
    python examples/measure_business_outcomes.py --demo \\
        --output reports/evaluation-runs/business-demo-20260831/

`--demo` 的编排器时钟是冻住的，所以脚本会自动把冻住的那个时刻作为
`handoff_reference_time` 传进去；否则"出发"会早于"交接"，提前预订天数是负的。
理由见 `services/evaluation_business` 模块开头的「时钟」一节。
"""

from __future__ import annotations

import argparse
from datetime import datetime

from corporate_travel_agent.domain.models import AuditEvent, TripTask
from corporate_travel_agent.services.evaluation_business import (
    build_business_metrics_report,
    write_business_metrics_report,
)


def _collect(repository, limit: int) -> tuple[list[TripTask], dict[str, tuple[AuditEvent, ...]]]:
    tasks: list[TripTask] = []
    events: dict[str, tuple[AuditEvent, ...]] = {}
    for summary in repository.list_task_summaries(limit=limit):
        task = repository.get(summary.task_id)
        tasks.append(task)
        events[task.task_id] = repository.events(task.task_id)
    return tasks, events


def _demo_tasks(limit: int):
    """跑一小段演示流程：一单说了"去订了"没回来填单号、一单回填了订单号、
    一单看了没订、一单走例外审批。

    四条路径各一个，是为了让报告里的每个分母都非零——全都测不出来的报告
    看不出格式对不对。
    """
    from decimal import Decimal

    from corporate_travel_agent.demo import DEMO_CLOCK, build_demo_system, make_demo_request
    from corporate_travel_agent.domain.enums import PolicyOutcome

    workflow, _ = build_demo_system(clock=lambda: DEMO_CLOCK)

    handed = workflow.create_task(make_demo_request(task_id="demo-handed-off"))
    compliant = next(
        item
        for item in handed.options
        if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(handed.task_id, compliant.option_id)
    workflow.mark_handed_off(handed.task_id)

    confirmed = workflow.create_task(make_demo_request(task_id="demo-confirmed"))
    confirmed_option = next(
        item
        for item in confirmed.options
        if item.policy_decision.outcome is PolicyOutcome.COMPLIANT
    )
    workflow.select_option(confirmed.task_id, confirmed_option.option_id)
    # 直接从「可交接」回填：交接完成那一笔会自动补记。实付比方案价多 20，看差额那一行。
    workflow.confirm_booking(
        confirmed.task_id,
        order_references=["DEMO-PNR-1", "DEMO-HTL-1"],
        total_amount=confirmed_option.total_cost + Decimal("20"),
        currency=confirmed_option.currency,
        reported_by="E1001",
    )

    workflow.create_task(make_demo_request(task_id="demo-abandoned"))

    approval = workflow.create_task(make_demo_request(task_id="demo-approval"))
    exception = next(
        item
        for item in approval.options
        if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    )
    workflow.select_option(
        approval.task_id, exception.option_id, business_reason="客户会议改期，只剩这班"
    )

    tasks, events = _collect(workflow.tasks, limit)
    return tasks, events, DEMO_CLOCK


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure business outcomes from existing tasks and audit events."
    )
    parser.add_argument("--output", required=True, help="报告目录，必须事先不存在")
    parser.add_argument("--database-url", default=None, help="PostgreSQL 连接串")
    parser.add_argument("--demo", action="store_true", help="不读数据库，先跑一小段演示流程")
    parser.add_argument("--limit", type=int, default=500, help="最多读多少个最近任务")
    parser.add_argument(
        "--handoff-reference-time",
        default=None,
        help="冻了时钟的跑法把冻住的时刻传进来（ISO 8601），用于算提前预订天数",
    )
    args = parser.parse_args()

    if args.demo == bool(args.database_url):
        parser.error("必须二选一：--database-url 或 --demo")

    reference: datetime | None = None
    if args.handoff_reference_time:
        reference = datetime.fromisoformat(args.handoff_reference_time)

    if args.demo:
        tasks, events, demo_clock = _demo_tasks(args.limit)
        reference = reference or demo_clock
    else:
        from corporate_travel_agent.services.sqlalchemy_repository import (
            SQLAlchemyTaskRepository,
        )

        repository = SQLAlchemyTaskRepository(args.database_url)
        try:
            tasks, events = _collect(repository, args.limit)
        finally:
            repository.dispose()

    report = build_business_metrics_report(
        tasks, events, handoff_reference_time=reference
    )
    output_root = write_business_metrics_report(report, args.output)

    print(f"tasks: {report.task_count}")
    for name, metric in report.metrics.items():
        value = "unavailable" if metric.value is None else f"{metric.value:.4f}"
        print(f"  {name}: {value}")
    print(f"written: {output_root}")


if __name__ == "__main__":
    main()
