"""差旅任务：创建（结构化 / 自然语言 / 流式）、跟进、选方案、交接、回填，以及它的记录视图。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from threading import Thread
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from corporate_travel_agent.agent.orchestrator import WorkflowError
from corporate_travel_agent.api.deps import (
    CurrentIdentity,
    Runtime,
    bounded_limit,
    can_read_task,
    requester_id,
    require_can_create,
    require_can_operate,
    run,
    visible_task,
)
from corporate_travel_agent.api.runtime import ApiRuntime
from corporate_travel_agent.api.schemas import (
    AuditEventOut,
    BookingConfirmationCreate,
    InventorySnapshotResponse,
    MessageCreate,
    NaturalLanguageTripCreate,
    OptionSelection,
    TaskResponse,
    TaskStepsResponse,
    TaskSummaryResponse,
    TripCreate,
)
from corporate_travel_agent.api.serializers import (
    public_audit_event,
    public_snapshot,
    public_task,
    public_task_summary,
    to_request,
)
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.domain.models import TripTask
from corporate_travel_agent.services.auth import Role, UserIdentity
from corporate_travel_agent.services.repositories import NotFoundError
from corporate_travel_agent.services.serialization import PayloadIncompatible
from corporate_travel_agent.services.task_projections import TaskSummary
from corporate_travel_agent.services.task_steps import collect_task_steps

LOGGER = logging.getLogger(__name__)
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

router = APIRouter(tags=["trip-tasks"])


# -- 创建 -----------------------------------------------------------------------


@router.post("/trip-tasks", response_model=TaskResponse)
def create_trip(payload: TripCreate, identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    """仅用结构化请求创建差旅任务。"""
    require_can_create(runtime, identity, payload.traveler_id)
    task_id = str(uuid4())
    request = to_request(payload, task_id=task_id, version=1)
    return run(
        runtime,
        lambda: runtime.workflow.create_task(request, requester_id=requester_id(identity)),
    )


@router.post("/agentic/trip-tasks", response_model=TaskResponse)
def create_agentic_trip(
    payload: NaturalLanguageTripCreate, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """用工具循环入口创建自然语言任务（唯一的自然语言入口，ADR-0003）。"""
    require_can_create(runtime, identity, payload.traveler_id)
    return run(
        runtime,
        lambda: runtime.workflow.create_task_from_agentic_message(
            payload.message,
            traveler_id=payload.traveler_id,
            requester_id=requester_id(identity),
        ),
    )


@router.post("/agentic/trip-tasks/stream")
def create_agentic_trip_stream(
    payload: NaturalLanguageTripCreate, identity: CurrentIdentity, runtime: Runtime
) -> StreamingResponse:
    """流式创建：SSE 一步一步推过程记录，最后给完整任务。

    降低的是**感知**延迟：任务总耗时不变，但用户 1 秒内就能看到"正在搜索北京→上海"。
    事件流：`accepted`（带 task_id）→ 若干 `step`（和 `GET /steps` 同一结构）→
    `task`（完整任务）或 `error` → `done`。
    """
    require_can_create(runtime, identity, payload.traveler_id)
    requester = requester_id(identity)
    task_id = str(uuid4())
    return StreamingResponse(
        _agentic_event_stream(
            runtime,
            task_id,
            lambda: runtime.workflow.create_task_from_agentic_message(
                payload.message,
                traveler_id=payload.traveler_id,
                task_id=task_id,
                requester_id=requester,
            ),
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/agentic/trip-tasks/{task_id}/messages/stream")
def submit_agentic_message_stream(
    task_id: str, payload: MessageCreate, identity: CurrentIdentity, runtime: Runtime
) -> StreamingResponse:
    """流式跟进：语义同上，作用在已有任务上。"""
    return StreamingResponse(
        _agentic_event_stream(
            runtime,
            task_id,
            _agentic_message_operation(runtime, task_id, payload.message, identity),
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/agentic/trip-tasks/{task_id}/messages", response_model=TaskResponse)
def submit_agentic_message(
    task_id: str, payload: MessageCreate, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """向工具循环任务提交跟进消息；已订任务上的消息读成变更请求。"""
    return run(runtime, _agentic_message_operation(runtime, task_id, payload.message, identity))


def _agentic_message_operation(
    runtime: ApiRuntime, task_id: str, message: str, identity: UserIdentity
) -> Callable[[], TripTask]:
    """跟进消息落到哪条链路：订好之前是工具循环续聊，订好之后是变更意图。

    已回填订单号的任务不能再重跑规划——原任务一个字不动。它上面的聊天读成一条
    变更请求（会议改期 / 航变自述 / 取消），落到差旅事件；改期时返回的是新开的改期任务。
    """
    task = visible_task(runtime, task_id, identity)
    require_can_operate(identity, task)
    workflow = runtime.workflow
    if task.state is TaskState.BOOKING_CONFIRMED:
        reporter = requester_id(identity) or identity.user_id
        return lambda: workflow.submit_change_message(task_id, message, reported_by=reporter)
    return lambda: workflow.submit_agentic_message(task_id, message)


def _sse_event(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _live_steps(runtime: ApiRuntime, task_id: str) -> list[dict[str, Any]]:
    """轮询用：任务还在跑时读它当前的步骤。读不到（还没建 / 正在写）就给空，下一轮再看。"""
    tasks = runtime.workflow.tasks
    try:
        task = tasks.get(task_id)
        return collect_task_steps(
            task, events=tasks.events(task_id), snapshots=tasks.snapshots(task_id)
        )
    except Exception:  # noqa: BLE001 - 工作线程正在写任务，读到一半失败是预期内的
        return []


def _agentic_event_stream(
    runtime: ApiRuntime, task_id: str, operation: Callable[[], TripTask]
) -> Iterator[str]:
    """把一次编排调用放进工作线程，边跑边把新增步骤推出去。

    步骤靠**轮询任务本身**取得（每次工具调用后任务都会持久化一次），不另起一条事件总线；
    增量按步骤数量切。工作线程写、这里读，偶尔读到写了一半的状态由 `_live_steps` 当空处理。
    """
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["task"] = operation()
        except Exception as exc:  # noqa: BLE001 - 原样转成 error 事件
            box["error"] = exc

    worker = Thread(target=work, daemon=True)
    worker.start()
    yield _sse_event("accepted", {"task_id": task_id})
    sent = 0
    while True:
        worker.join(0.25)
        steps = _live_steps(runtime, task_id)
        for step in steps[sent:]:
            yield _sse_event("step", step)
        sent = max(sent, len(steps))
        if not worker.is_alive():
            break
    error = box.get("error")
    if error is not None:
        yield _sse_event("error", {"type": type(error).__name__, "detail": str(error)})
    else:
        yield _sse_event("task", public_task(runtime.workflow, box["task"]))
    yield _sse_event("done", {})


# -- 列表与详情 ---------------------------------------------------------------


@router.get("/trip-tasks", response_model=list[TaskSummaryResponse | TaskResponse])
def list_trips(
    identity: CurrentIdentity,
    runtime: Runtime,
    summary: bool = True,
    limit: int = 100,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """列出调用方可视的任务。默认紧凑摘要；``summary=false`` 返回完整公开任务文档。"""
    bounded_limit(limit)
    summaries = _visible_task_summaries(runtime, identity, state=state, limit=limit)
    if summary:
        return [public_task_summary(item) for item in summaries]
    tasks: list[dict[str, Any]] = []
    for item in summaries:
        try:
            task = runtime.workflow.tasks.get(item.task_id)
        except NotFoundError:
            continue
        except PayloadIncompatible as exc:
            # 一行旧载荷不该让整张列表 500：摘要（投影列）照常可见，全文跳过并记日志。
            LOGGER.warning("skipping task %s in full listing: %s", item.task_id, exc)
            continue
        if can_read_task(identity, task):
            tasks.append(public_task(runtime.workflow, task))
    return tasks


def _visible_task_summaries(
    runtime: ApiRuntime, identity: UserIdentity, *, state: str | None, limit: int
) -> tuple[TaskSummary, ...]:
    tasks = runtime.workflow.tasks
    if identity.has_role(Role.ADMIN):
        return tasks.list_task_summaries(state=state, limit=limit)
    if identity.has_role(Role.EMPLOYEE) and identity.employee_id:
        # 我是旅行者的，加上我替别人发起的。
        return tasks.list_task_summaries(
            involving_employee_id=identity.employee_id, state=state, limit=limit
        )
    if identity.has_role(Role.APPROVER):
        return tasks.list_task_summaries(manager_id=identity.user_id, state=state, limit=limit)
    return ()


@router.get("/trip-tasks/{task_id}", response_model=TaskResponse)
def get_trip(task_id: str, identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    """获取单个任务的公开视图。"""
    return public_task(runtime.workflow, visible_task(runtime, task_id, identity))


# -- 工作流操作 ---------------------------------------------------------------


@router.post("/trip-tasks/{task_id}/structured-request", response_model=TaskResponse)
def submit_structured_request(
    task_id: str, payload: TripCreate, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """在澄清失败后用完整结构化请求继续。"""

    def operation() -> TripTask:
        task = visible_task(runtime, task_id, identity)
        require_can_operate(identity, task)
        if payload.traveler_id != task.employee.employee_id:
            raise ValueError("traveler_id cannot change within an existing task")
        request = to_request(
            payload,
            task_id=task_id,
            version=1 if task.request is None else task.request.version + 1,
        )
        return runtime.workflow.complete_with_structured_request(task_id, request)

    return run(runtime, operation)


@router.post("/trip-tasks/{task_id}/select-option", response_model=TaskResponse)
def select_option(
    task_id: str, payload: OptionSelection, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """选中某个 TravelOption。"""
    require_can_operate(identity, visible_task(runtime, task_id, identity))
    return run(
        runtime,
        lambda: runtime.workflow.select_option(
            task_id, payload.option_id, business_reason=payload.business_reason
        ),
    )


@router.post("/trip-tasks/{task_id}/replan", response_model=TaskResponse)
def replan(task_id: str, identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    """触发重试或重规划。"""
    require_can_operate(identity, visible_task(runtime, task_id, identity))
    return run(runtime, lambda: runtime.workflow.retry_or_replan(task_id))


@router.post("/trip-tasks/{task_id}/revise-request", response_model=TaskResponse)
def revise_request(
    task_id: str, payload: TripCreate, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """用新版结构化请求修订并重搜。"""
    task = visible_task(runtime, task_id, identity)
    require_can_operate(identity, task)
    if payload.traveler_id != task.employee.employee_id:
        raise HTTPException(status_code=409, detail="traveler_id cannot change")
    if task.request is None:
        raise HTTPException(status_code=409, detail="Draft task has no request to revise")
    request = to_request(payload, task_id=task_id, version=task.request.version + 1)
    return run(runtime, lambda: runtime.workflow.revise_request(task_id, request))


@router.post("/trip-tasks/{task_id}/handoff-completed", response_model=TaskResponse)
def handoff_completed(
    task_id: str, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """标记已完成向 Provider 的交接。"""
    require_can_operate(identity, visible_task(runtime, task_id, identity))
    return run(runtime, lambda: runtime.workflow.mark_handed_off(task_id))


@router.post("/trip-tasks/{task_id}/booking-confirmation", response_model=TaskResponse)
def confirm_booking(
    task_id: str,
    payload: BookingConfirmationCreate,
    identity: CurrentIdentity,
    runtime: Runtime,
) -> dict[str, Any]:
    """员工回填"我订好了"：订单号、实付金额。

    这是交接之后系统唯一能拿到的"真的订了"的证据。**只是自述**——系统核不了订单号和
    金额；`source` 固定为 `SELF_REPORTED`。一个任务只能回填一次，填错了开新任务。
    从 `READY_FOR_HANDOFF` 直接回填也行：后端会先记一笔交接完成，再记确认。
    """
    task = visible_task(runtime, task_id, identity)
    require_can_operate(identity, task)
    reported_by = identity.employee_id or identity.user_id
    return run(
        runtime,
        lambda: runtime.workflow.confirm_booking(
            task_id,
            order_references=payload.order_references,
            total_amount=payload.total_amount,
            currency=payload.currency,
            reported_by=reported_by,
            booked_at=payload.booked_at,
            note=payload.note,
        ),
    )


# -- 记录视图 ---------------------------------------------------------------


@router.get("/trip-tasks/{task_id}/audit-events", response_model=list[AuditEventOut])
def audit_events(task_id: str, identity: CurrentIdentity, runtime: Runtime) -> list[dict[str, Any]]:
    """列出任务审计事件。"""
    visible_task(runtime, task_id, identity)
    try:
        return [public_audit_event(item) for item in runtime.workflow.tasks.events(task_id)]
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/trip-tasks/{task_id}/steps", response_model=TaskStepsResponse)
def task_steps(task_id: str, identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    """任务从建到现在的每一步，按先后整理。只收集和排序，不改任务、不重算任何结论。"""
    task = visible_task(runtime, task_id, identity)
    tasks = runtime.workflow.tasks
    try:
        events = tasks.events(task_id)
        snapshots = tasks.snapshots(task_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    steps = collect_task_steps(task, events=events, snapshots=snapshots)
    return {"task_id": task_id, "count": len(steps), "steps": steps}


@router.get("/trip-tasks/{task_id}/options/{option_id}/provenance")
def option_provenance(
    task_id: str, option_id: str, identity: CurrentIdentity, runtime: Runtime
) -> dict[str, Any]:
    """一条方案的依据链：每一步凭什么，以及哪些地方说不出来（`gaps`）。自由形状的记录。"""
    visible_task(runtime, task_id, identity)
    try:
        return runtime.workflow.option_provenance_record(task_id, option_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/trip-tasks/{task_id}/provenance-check")
def provenance_check(task_id: str, identity: CurrentIdentity, runtime: Runtime) -> dict[str, Any]:
    """重算依据链，和交接时钉住的指纹比对。对不上就是有东西被改过。"""
    visible_task(runtime, task_id, identity)
    try:
        return runtime.workflow.verify_provenance(task_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/trip-tasks/{task_id}/inventory-snapshots", response_model=list[InventorySnapshotResponse]
)
def inventory_snapshots(
    task_id: str, identity: CurrentIdentity, runtime: Runtime
) -> list[dict[str, Any]]:
    """列出任务相关库存快照（脱敏公开视图）。"""
    visible_task(runtime, task_id, identity)
    try:
        return [public_snapshot(item) for item in runtime.workflow.tasks.snapshots(task_id)]
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
