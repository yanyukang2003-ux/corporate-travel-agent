"""从领域聚合派生可查询任务投影字段（列表摘要、重试元数据等）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from corporate_travel_agent.domain.enums import ApprovalStatus, TaskState
from corporate_travel_agent.domain.models import TripTask
from corporate_travel_agent.services.provider_resilience import PROVIDER_RETRY_METADATA_KEY
from corporate_travel_agent.services.serialization import SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class TaskSummary:
    """任务列表/查询用的只读摘要视图。"""

    task_id: str
    state: str
    employee_id: str
    manager_id: str
    policy_snapshot_id: str
    selected_option_id: str | None
    clarification_rounds: int
    next_retry_at: datetime | None
    delayed_retry_count: int
    payload_schema_version: int
    revision: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    option_count: int = 0
    request_version: int | None = None
    failure: str | None = None
    #: 现在轮到谁批。只有 WAITING_FOR_APPROVAL 且审批单还挂着时才有值。
    pending_approver_id: str | None = None


def pending_approver_id(task: TripTask) -> str | None:
    """当前该批的人；不在等审批就是 None。分级审批走到第二级时这里跟着换。"""
    approval = task.approval
    if task.state is not TaskState.WAITING_FOR_APPROVAL or approval is None:
        return None
    if approval.status is not ApprovalStatus.PENDING:
        return None
    return approval.approver_id


def projection_fields(task: TripTask) -> dict[str, Any]:
    """从 TripTask 提取入库/索引所需的投影字段（员工、策略、延迟重试等）。"""
    retry = _provider_retry_metadata(task)
    delayed = retry.get("delayed_attempts_completed", 0)
    try:
        delayed_retry_count = max(int(delayed), 0)
    except (TypeError, ValueError):
        delayed_retry_count = 0
    return {
        "employee_id": task.employee.employee_id,
        "manager_id": task.employee.manager_id,
        "policy_snapshot_id": task.policy_snapshot_id,
        "selected_option_id": task.selected_option_id,
        "clarification_rounds": task.clarification_rounds,
        "next_retry_at": _as_datetime(retry.get("next_retry_at")),
        "delayed_retry_count": delayed_retry_count,
        "retry_lease_owner": retry.get("lease_owner"),
        "retry_lease_until": _as_datetime(retry.get("lease_until")),
        "retry_attempt_token": retry.get("attempt_token"),
        "payload_schema_version": SCHEMA_VERSION,
        "pending_approver_id": pending_approver_id(task),
    }


def summarize_task(
    task: TripTask,
    *,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> TaskSummary:
    """将 TripTask 压缩为 TaskSummary，供 API 列表与运营查询使用。"""
    fields = projection_fields(task)
    return TaskSummary(
        task_id=task.task_id,
        state=task.state.value if isinstance(task.state, TaskState) else str(task.state),
        employee_id=fields["employee_id"],
        manager_id=fields["manager_id"],
        policy_snapshot_id=fields["policy_snapshot_id"],
        selected_option_id=fields["selected_option_id"],
        clarification_rounds=fields["clarification_rounds"],
        next_retry_at=fields["next_retry_at"],
        delayed_retry_count=fields["delayed_retry_count"],
        payload_schema_version=fields["payload_schema_version"],
        revision=task.persistence_revision,
        created_at=created_at,
        updated_at=updated_at,
        option_count=len(task.options),
        request_version=task.request.version if task.request else None,
        failure=task.failure,
        pending_approver_id=fields["pending_approver_id"],
    )


def _provider_retry_metadata(task: TripTask) -> dict[str, Any]:
    value = task.metadata.get(PROVIDER_RETRY_METADATA_KEY)
    return value if isinstance(value, dict) else {}


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
