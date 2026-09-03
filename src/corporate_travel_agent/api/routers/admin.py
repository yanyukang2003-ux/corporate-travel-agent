"""管理端：费控对账、发件箱、跨任务审计、业务指标、谁在哪、预算消耗。全部只对管理员开放。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException

from corporate_travel_agent.api.deps import CurrentIdentity, Runtime, bounded_limit, require_admin
from corporate_travel_agent.api.schemas import (
    AuditEventOut,
    BudgetsResponse,
    DutyOfCareResponse,
    ExpenseImportRequest,
    ExpenseImportResponse,
    ExpenseRecordOut,
    OutboxDispatchRequest,
    OutboxDispatchResponse,
    OutboxEventOut,
)
from corporate_travel_agent.api.serializers import public_audit_event
from corporate_travel_agent.domain.models import AuditEvent, TripTask
from corporate_travel_agent.services.business_metrics import (
    METRIC_LABELS,
    build_business_metrics_report,
)
from corporate_travel_agent.services.duty_of_care import whereabouts
from corporate_travel_agent.services.expense_reconciliation import (
    reconcile_expenses,
    record_from_payload,
)
from corporate_travel_agent.services.repositories import NotFoundError

router = APIRouter(tags=["admin"])


@router.post("/expenses/import", response_model=ExpenseImportResponse)
def import_expenses(
    payload: ExpenseImportRequest, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """导入费控记录并逐条对账。同一个 expense_id 重复导入会被跳过。

    对上了的自述从此算"核实过"；对不上的两边都留着；本系统里没有对应确认的，就是渠道外
    预订——`GET /metrics/business` 的 `off_channel_expense_rate` 从这里来。
    """
    require_admin(identity)
    try:
        records = [
            record_from_payload({"source_system": payload.source_system, **item})
            for item in payload.records
        ]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    workflow = runtime.workflow
    report = reconcile_expenses(
        records,
        repository=workflow.tasks,
        store=runtime.expense_store,
        reconcile=workflow.reconcile_expense,
        now=workflow.clock,
    )
    return report.as_dict()


@router.get("/expenses/records", response_model=list[ExpenseRecordOut])
def expense_records(
    identity: CurrentIdentity, runtime: Runtime, limit: int = 100
) -> list[dict[str, Any]]:
    """导入过的费控记录及对账结果。"""
    bounded_limit(limit, high=1000)
    require_admin(identity)
    return [
        {
            "expense_id": item.record.expense_id,
            "employee_id": item.record.employee_id,
            "amount": str(item.record.amount),
            "currency": item.record.currency,
            "expensed_at": item.record.expensed_at,
            "order_references": list(item.record.order_references),
            "source_system": item.record.source_system,
            "cost_center": item.record.cost_center,
            "description": item.record.description,
            "status": item.status.value,
            "matched_task_id": item.matched_task_id,
            "note": item.note,
            "imported_at": item.imported_at,
        }
        for item in runtime.expense_store.list_records(limit=limit)
    ]


@router.post("/outbox/dispatch", response_model=OutboxDispatchResponse)
def dispatch_outbox(
    payload: OutboxDispatchRequest, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """手动投递一轮发件箱。生产里由 `examples/run_outbox_worker.py` 循环做同一件事。"""
    require_admin(identity)
    report = runtime.outbox_dispatcher.dispatch_once(limit=payload.limit)
    return {
        **report.as_dict(),
        "channel": runtime.outbox_dispatcher.default_channel.name,
        "unpublished_count": runtime.outbox_store.unpublished_count(),
    }


@router.get("/outbox/events", response_model=list[OutboxEventOut])
def outbox_events(
    identity: CurrentIdentity, runtime: Runtime, limit: int = 50
) -> list[dict[str, Any]]:
    """最近的发件箱事件（已发布和未发布都有），看投递有没有卡住。"""
    bounded_limit(limit)
    require_admin(identity)
    max_attempts = runtime.outbox_dispatcher.max_attempts
    return [
        {
            "event_id": item.event_id,
            "event_type": item.event_type,
            "aggregate_type": item.aggregate_type,
            "aggregate_id": item.aggregate_id,
            "created_at": item.created_at,
            "published_at": item.published_at,
            "attempt_count": item.attempt_count,
            "last_error": item.last_error,
            "dead_lettered": item.published_at is None and item.attempt_count >= max_attempts,
            "payload": item.payload,
        }
        for item in runtime.outbox_store.list_recent(limit=limit)
    ]


@router.get("/audit-events", response_model=list[AuditEventOut])
def recent_audit_events(
    identity: CurrentIdentity, runtime: Runtime, limit: int = 50
) -> list[dict[str, Any]]:
    """近期审计事件（跨任务）。走任务投影而不是全表，扫描量由 limit 约束；不是完整审计导出。"""
    bounded_limit(limit)
    require_admin(identity)
    tasks = runtime.workflow.tasks
    events: list[dict[str, Any]] = []
    for summary in tasks.list_task_summaries(limit=limit):
        try:
            events.extend(public_audit_event(item) for item in tasks.events(summary.task_id))
        except NotFoundError:
            continue
    events.sort(key=lambda item: item["created_at"], reverse=True)
    return events[:limit]


@router.get("/metrics/business")
def business_metrics(
    identity: CurrentIdentity, runtime: Runtime, limit: int = 200
) -> dict[str, Any]:
    """业务结果指标：用了多久、提前多少天订、多少单超标、员工有没有真去下单。

    读最近 ``limit`` 个任务的完整聚合和审计事件；运营看板用的近期视图，不是评测产物。
    线上编排器用真实时间，所以不传 ``handoff_reference_time``；冻了时钟的离线跑法必须自己传。
    """
    bounded_limit(limit, high=200)
    require_admin(identity)
    repository = runtime.workflow.tasks
    tasks: list[TripTask] = []
    events: dict[str, tuple[AuditEvent, ...]] = {}
    for summary in repository.list_task_summaries(limit=limit):
        try:
            tasks.append(repository.get(summary.task_id))
            events[summary.task_id] = repository.events(summary.task_id)
        except NotFoundError:
            continue
    report = build_business_metrics_report(
        tasks, events, expense_records=runtime.expense_store.list_records(limit=limit)
    )
    payload = report.model_dump(mode="json")
    payload["labels"] = dict(METRIC_LABELS)
    return payload


@router.get("/duty-of-care", response_model=DutyOfCareResponse)
def duty_of_care(
    identity: CurrentIdentity,
    runtime: Runtime,
    at: datetime | None = None,
    include_completed: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """谁在哪：从已确认行程的航段推出每位旅行者此刻的位置。没确认的行程不在这里。"""
    require_admin(identity)
    bounded_limit(limit, high=2000)
    moment = at or runtime.workflow.clock()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    rows = whereabouts(
        runtime.workflow.trips.list_all(limit=limit),
        at=moment,
        include_completed=include_completed,
    )
    return {
        "at": moment,
        "travelers": [
            {**asdict(item), "status": item.status.value, "trip_status": item.trip_status.value}
            for item in rows
        ],
    }


@router.get("/budgets", response_model=BudgetsResponse)
def budgets(identity: CurrentIdentity, runtime: Runtime, limit: int = 200) -> dict[str, Any]:
    """成本中心预算消耗：政策里的额度、账本里确认过的支出、已交接还没确认的在途金额。"""
    require_admin(identity)
    bounded_limit(limit)
    workflow = runtime.workflow
    policy = workflow.policies.current()
    ledger = workflow.budget_ledger
    committed: dict[tuple[str, str], Decimal] = {}
    for summary in workflow.tasks.list_task_summaries(limit=limit):
        if summary.state not in {"READY_FOR_HANDOFF", "HANDED_OFF"}:
            continue
        try:
            task = workflow.tasks.get(summary.task_id)
        except NotFoundError:
            continue
        option = task.selected_option()
        if option is None or task.employee.cost_center is None:
            continue
        key = (task.employee.cost_center, option.currency)
        committed[key] = committed.get(key, Decimal("0")) + option.total_cost
    lines = []
    for budget in policy.cost_center_budgets.values():
        spent = (
            ledger.spent(
                budget.cost_center,
                currency=budget.currency,
                period_from=budget.period_from,
                period_to=budget.period_to,
            )
            if ledger is not None
            else None
        )
        in_flight = committed.get((budget.cost_center, budget.currency), Decimal("0"))
        lines.append(
            {
                "cost_center": budget.cost_center,
                "currency": budget.currency,
                "limit": str(budget.amount),
                "period_from": budget.period_from,
                "period_to": budget.period_to,
                "spent": str(spent) if spent is not None else None,
                "committed": str(in_flight),
                "remaining": str(budget.amount - spent) if spent is not None else None,
                "ledger_available": ledger is not None,
            }
        )
    return {"policy_snapshot_id": policy.snapshot_id, "budgets": lines}
