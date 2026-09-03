"""例外审批：收件箱与决定。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from corporate_travel_agent.api.deps import (
    CurrentIdentity,
    Runtime,
    bounded_limit,
    require_can_approve,
    run,
    visible_task,
)
from corporate_travel_agent.api.schemas import ApprovalDecision, TaskResponse, TaskSummaryResponse
from corporate_travel_agent.api.serializers import public_task_summary
from corporate_travel_agent.services.auth import Role

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/inbox", response_model=list[TaskSummaryResponse])
def approval_inbox(
    identity: CurrentIdentity, runtime: Runtime, limit: int = 100
) -> list[dict[str, Any]]:
    """当前审批人的待办例外审批（基于投影）。

    收件箱按"当前待谁批"查，不按直属经理：分级审批走到第二级时，该看见它的是财务。
    """
    bounded_limit(limit)
    if not (identity.has_role(Role.APPROVER) or identity.has_role(Role.ADMIN)):
        raise HTTPException(status_code=403, detail="Approver role required")
    tasks = runtime.workflow.tasks
    if identity.has_role(Role.ADMIN):
        summaries = tasks.list_task_summaries(state="WAITING_FOR_APPROVAL", limit=limit)
    else:
        summaries = tasks.list_task_summaries(
            pending_approver_id=identity.user_id, state="WAITING_FOR_APPROVAL", limit=limit
        )
    return [public_task_summary(item) for item in summaries]


@router.post("/{task_id}/decision", response_model=TaskResponse)
def decide_approval(
    task_id: str, payload: ApprovalDecision, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """提交例外审批决定。审批身份由令牌派生，不信任请求体。"""
    task = visible_task(runtime, task_id, identity)
    approver_id = payload.approver_id or task.employee.manager_id
    if runtime.auth_service.enabled:
        require_can_approve(identity, task)
        if payload.approver_id and payload.approver_id != identity.user_id:
            raise HTTPException(status_code=403, detail="Approver identity mismatch")
        approver_id = identity.user_id
    return run(
        runtime,
        lambda: runtime.workflow.decide_approval(
            task_id, approver_id=approver_id, approved=payload.approved, reason=payload.reason
        ),
    )
