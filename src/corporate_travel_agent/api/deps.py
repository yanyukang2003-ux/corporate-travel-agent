"""路由的依赖：运行时、当前身份、资源级授权、领域异常到 HTTP 状态码的映射。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from corporate_travel_agent.agent.orchestrator import LanguageModelUnavailable, WorkflowError
from corporate_travel_agent.api.runtime import ApiRuntime
from corporate_travel_agent.api.serializers import public_task
from corporate_travel_agent.domain.models import Trip, TripTask
from corporate_travel_agent.services.auth import AuthenticationFailed, Role, UserIdentity
from corporate_travel_agent.services.repositories import ConcurrentUpdateError, NotFoundError
from corporate_travel_agent.services.serialization import PayloadIncompatible
from corporate_travel_agent.services.task_projections import pending_approver_id


def get_runtime(request: Request) -> ApiRuntime:
    """这次请求所属应用的运行时——`create_app` 把它挂在 `app.state.runtime` 上。"""
    runtime = request.app.state.runtime
    assert isinstance(runtime, ApiRuntime)
    return runtime


Runtime = Annotated[ApiRuntime, Depends(get_runtime)]

bearer_scheme = HTTPBearer(auto_error=False)


def current_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    runtime: Runtime,
) -> UserIdentity:
    """从 Bearer Token 解析当前用户身份。"""
    token = credentials.credentials if credentials else None
    try:
        return runtime.auth_service.authenticate(token)
    except AuthenticationFailed as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentIdentity = Annotated[UserIdentity, Depends(current_identity)]


# -- 授权 ------------------------------------------------------------------------


def requester_id(identity: UserIdentity) -> str | None:
    """记到任务上的发起人：员工记员工 ID，管理员记用户 ID；鉴权关闭的开发身份不记。"""
    if identity.employee_id:
        return identity.employee_id
    if identity.has_role(Role.ADMIN) and identity.user_id != "development-system":
        return identity.user_id
    return None


def can_read_task(identity: UserIdentity, task: TripTask) -> bool:
    """旅行者本人、发起人（代订的助理）、直属经理或当前审批人、管理员可读。"""
    return (
        identity.has_role(Role.ADMIN)
        or (
            identity.has_role(Role.EMPLOYEE)
            and identity.employee_id in {task.employee.employee_id, task.requested_by}
        )
        or (
            identity.has_role(Role.APPROVER)
            and identity.user_id in {task.employee.manager_id, pending_approver_id(task)}
        )
    )


def visible_task(runtime: ApiRuntime, task_id: str, identity: UserIdentity) -> TripTask:
    """按权限取任务；不可见则 404；库里的旧载荷升不上来给 500 但说清楚该做什么。"""
    try:
        task = runtime.workflow.tasks.get(task_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except PayloadIncompatible as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Stored task payload predates the current schema ({exc.version}); "
                "run examples/upgrade_task_payloads.py --apply"
            ),
        ) from exc
    if not can_read_task(identity, task):
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def require_can_create(runtime: ApiRuntime, identity: UserIdentity, traveler_id: str) -> None:
    """可为该出行人创建任务：本人、旅行者委托名单上的人、管理员。"""
    if identity.has_role(Role.ADMIN):
        return
    if identity.has_role(Role.EMPLOYEE) and identity.employee_id:
        if identity.employee_id == traveler_id:
            return
        if runtime.workflow.employees.may_book_for(identity.employee_id, traveler_id):
            return
    raise HTTPException(status_code=403, detail="Cannot create a task for this traveler")


def require_can_operate(identity: UserIdentity, task: TripTask) -> None:
    """可操作该任务（消息 / 选方案等）：旅行者本人、发起人、管理员。"""
    if identity.has_role(Role.ADMIN):
        return
    if identity.has_role(Role.EMPLOYEE) and identity.employee_id in {
        task.employee.employee_id,
        task.requested_by,
    }:
        return
    raise HTTPException(
        status_code=403, detail="Only the traveler or the requester can modify this task"
    )


def require_can_approve(identity: UserIdentity, task: TripTask) -> None:
    """必须是**当前这一级**该批的人：走到财务那一级时直属经理不能再替财务批。"""
    pending = pending_approver_id(task)
    if identity.has_role(Role.APPROVER) and pending is not None and identity.user_id == pending:
        return
    if (
        pending is None
        and identity.has_role(Role.APPROVER)
        and identity.user_id == task.employee.manager_id
    ):
        # 不在等审批：让编排器给出准确的 409（"没有待处理的审批"），而不是 403。
        return
    raise HTTPException(status_code=403, detail="Task is outside this approver's scope")


def require_admin(identity: UserIdentity) -> None:
    if not identity.has_role(Role.ADMIN):
        raise HTTPException(status_code=403, detail="Admin role required")


def visible_trip(runtime: ApiRuntime, trip_id: str, identity: UserIdentity) -> Trip:
    try:
        trip = runtime.workflow.trips.get(trip_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail="Trip not found") from exc
    if identity.has_role(Role.ADMIN):
        return trip
    if identity.has_role(Role.EMPLOYEE) and identity.employee_id in {
        trip.traveler_id,
        trip.requester_id,
    }:
        return trip
    raise HTTPException(status_code=404, detail="Trip not found")


def bounded_limit(value: int, *, low: int = 1, high: int = 500) -> int:
    if not low <= value <= high:
        raise HTTPException(status_code=400, detail=f"limit must be between {low} and {high}")
    return value


# -- 领域异常 → HTTP --------------------------------------------------------------


def run(runtime: ApiRuntime, operation: Callable[[], TripTask]) -> dict[str, Any]:
    """执行一次编排器操作并映射领域异常为 HTTP 状态码。"""
    try:
        return public_task(runtime.workflow, operation())
    except LanguageModelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ConcurrentUpdateError, WorkflowError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def http_error_for(exc: Exception) -> HTTPException:
    """差旅接口用的同一张映射表，给不返回任务的操作。"""
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))
