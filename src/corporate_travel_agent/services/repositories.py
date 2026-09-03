"""任务/员工/策略仓储端口与内存实现（协议、并发控制、延迟重试认领）。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from corporate_travel_agent.domain.models import (
    AuditEvent,
    EmployeeProfileSnapshot,
    InventorySnapshot,
    PolicySnapshot,
    Trip,
    TripTask,
)
from corporate_travel_agent.services.outbox_events import InMemoryOutboxStore, OutboxEventDraft
from corporate_travel_agent.services.task_projections import TaskSummary

if TYPE_CHECKING:
    from corporate_travel_agent.services.trips import TripRepository


class NotFoundError(KeyError):
    """请求的任务、员工或策略快照不存在。"""


class ConcurrentUpdateError(RuntimeError):
    """乐观并发：持久化修订号冲突。"""


class SnapshotConflictError(RuntimeError):
    """库存快照 ID 冲突或不可变约束被破坏。"""


class EmployeeDirectory(Protocol):
    """员工目录端口：差标、审批链、委托名单都从这里读。"""

    def snapshot(self, employee_id: str) -> EmployeeProfileSnapshot: ...

    def knows(self, employee_id: str) -> bool: ...

    def delegators_of(self, employee_id: str) -> tuple[str, ...]: ...

    def may_book_for(self, requester_id: str, traveler_id: str) -> bool: ...


class PolicyRepository(Protocol):
    """政策快照端口：新任务绑当前快照，旧任务按自己的快照 ID 读历史版本。"""

    def current(self) -> PolicySnapshot: ...

    def snapshot(self, snapshot_id: str) -> PolicySnapshot: ...


class TaskRepository(Protocol):
    """任务聚合仓储端口：CRUD、摘要查询、延迟重试认领与审计/快照附属数据。"""

    backend_name: str

    def add(self, task: TripTask) -> None: ...

    def get(self, task_id: str) -> TripTask: ...

    def list_tasks(self) -> tuple[TripTask, ...]: ...

    def list_task_summaries(
        self,
        *,
        employee_id: str | None = None,
        manager_id: str | None = None,
        state: str | None = None,
        pending_approver_id: str | None = None,
        involving_employee_id: str | None = None,
        limit: int = 100,
    ) -> tuple[TaskSummary, ...]: ...

    def list_by_employee(self, employee_id: str, *, limit: int = 100) -> tuple[TripTask, ...]: ...

    def list_by_state(self, state: str, *, limit: int = 100) -> tuple[TripTask, ...]: ...

    def list_by_states(
        self,
        states: Sequence[str],
        *,
        updated_before: datetime | None = None,
        limit: int = 100,
    ) -> tuple[TripTask, ...]:
        """按几个状态一起查，可加"最后更新早于某刻"。重启恢复扫描靠它不用全表反序列化。"""
        ...

    def list_due_provider_retries(
        self,
        *,
        now: datetime,
        limit: int = 20,
    ) -> tuple[TripTask, ...]: ...

    def claim_due_provider_retries(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 20,
    ) -> tuple[TripTask, ...]: ...

    def release_provider_retry_claim(self, task: TripTask, *, attempt_token: str) -> bool: ...

    def record(
        self,
        task: TripTask,
        event: AuditEvent,
        *,
        outbox_events: Sequence[OutboxEventDraft] = (),
        preceding: Sequence[AuditEvent] = (),
    ) -> None: ...

    def add_with_trip(
        self,
        task: TripTask,
        *,
        trip: Trip,
        trip_is_new: bool,
        trips: TripRepository,
    ) -> None:
        """新任务和它所属的差旅一起落库。

        两张表在同一个库里（同一个引擎）就是**同一笔事务**，要么一起成、要么一起回滚；
        不在同一个库里（任务在 SQL、差旅在内存，单测常见）退回先任务后差旅。判断由实现做，
        调用方不必知道引擎是什么。
        """
        ...

    def record_with_trip(
        self,
        task: TripTask,
        event: AuditEvent,
        *,
        trip: Trip,
        trips: TripRepository,
        outbox_events: Sequence[OutboxEventDraft] = (),
        preceding: Sequence[AuditEvent] = (),
    ) -> None:
        """`record()` 加上差旅聚合的更新；事务边界同 `add_with_trip`。"""
        ...

    def events(self, task_id: str) -> tuple[AuditEvent, ...]: ...

    def add_snapshot(self, task_id: str, snapshot: InventorySnapshot) -> None: ...

    def snapshots(self, task_id: str) -> tuple[InventorySnapshot, ...]: ...


class InMemoryTaskRepository:
    """内存任务仓储，供单测与无数据库演示使用。"""

    backend_name = "memory"

    def __init__(self, *, outbox: InMemoryOutboxStore | None = None) -> None:
        #: 发件箱。`record()` 把事件草稿和任务更新一起写进来——内存版的"同一笔事务"。
        self.outbox = outbox or InMemoryOutboxStore()
        self._tasks: dict[str, TripTask] = {}
        self._events: dict[str, list[AuditEvent]] = {}
        self._snapshots: dict[str, dict[str, InventorySnapshot]] = {}
        self._created_at: dict[str, datetime] = {}
        self._updated_at: dict[str, datetime] = {}
        self._retry_claim_tokens: dict[str, str] = {}
        self._lock = RLock()

    def add(self, task: TripTask) -> None:
        if task.task_id in self._tasks:
            raise ValueError(f"Task {task.task_id} already exists")
        now = datetime.now()
        self._tasks[task.task_id] = task
        self._events[task.task_id] = []
        self._snapshots[task.task_id] = {}
        self._created_at[task.task_id] = now
        self._updated_at[task.task_id] = now

    def get(self, task_id: str) -> TripTask:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise NotFoundError(task_id) from exc

    def list_tasks(self) -> tuple[TripTask, ...]:
        return tuple(self._tasks.values())

    def list_task_summaries(
        self,
        *,
        employee_id: str | None = None,
        manager_id: str | None = None,
        state: str | None = None,
        pending_approver_id: str | None = None,
        involving_employee_id: str | None = None,
        limit: int = 100,
    ) -> tuple[TaskSummary, ...]:
        from corporate_travel_agent.services.task_projections import (
            pending_approver_id as pending_approver_of,
        )
        from corporate_travel_agent.services.task_projections import (
            summarize_task,
        )

        items: list[TaskSummary] = []
        for task in self._tasks.values():
            if employee_id is not None and task.employee.employee_id != employee_id:
                continue
            if manager_id is not None and task.employee.manager_id != manager_id:
                continue
            if state is not None and task.state.value != state:
                continue
            if pending_approver_id is not None and pending_approver_of(task) != pending_approver_id:
                continue
            # "和我有关"：我是旅行者，或者我是发起人（替别人订的）。
            if involving_employee_id is not None and involving_employee_id not in {
                task.employee.employee_id,
                task.requested_by,
            }:
                continue
            items.append(
                summarize_task(
                    task,
                    created_at=self._created_at.get(task.task_id),
                    updated_at=self._updated_at.get(task.task_id),
                )
            )
        items.sort(
            key=lambda item: (
                item.updated_at or datetime.min,
                item.task_id,
            ),
            reverse=True,
        )
        return tuple(items[: max(limit, 0)])

    def list_by_employee(self, employee_id: str, *, limit: int = 100) -> tuple[TripTask, ...]:
        return self._filtered(employee_id=employee_id, limit=limit)

    def list_by_state(self, state: str, *, limit: int = 100) -> tuple[TripTask, ...]:
        return self._filtered(state=state, limit=limit)

    def list_by_states(
        self,
        states: Sequence[str],
        *,
        updated_before: datetime | None = None,
        limit: int = 100,
    ) -> tuple[TripTask, ...]:
        # 内存版不按 updated_before 过滤：这里的更新时刻是本地墙钟，和调用方的业务时钟
        # 未必可比；调用方本来就会再核一遍活动时间。语义上是"候选集"，宁多勿漏。
        del updated_before
        wanted = set(states)
        with self._lock:
            matched = [task for task in self._tasks.values() if task.state.value in wanted]
        return tuple(matched[:limit])

    def list_due_provider_retries(
        self,
        *,
        now: datetime,
        limit: int = 20,
    ) -> tuple[TripTask, ...]:
        from corporate_travel_agent.domain.enums import TaskState
        from corporate_travel_agent.services.provider_resilience import (
            PROVIDER_RETRY_METADATA_KEY,
        )
        from corporate_travel_agent.services.task_projections import projection_fields

        due: list[tuple[datetime, TripTask]] = []
        for task in self._tasks.values():
            if task.state is not TaskState.WAITING_FOR_PROVIDER:
                continue
            metadata = task.metadata.get(PROVIDER_RETRY_METADATA_KEY)
            if not isinstance(metadata, dict) or metadata.get("status") != "scheduled":
                continue
            fields = projection_fields(task)
            next_retry_at = fields["next_retry_at"]
            if next_retry_at is None or next_retry_at > now:
                continue
            due.append((next_retry_at, task))
        due.sort(key=lambda item: (item[0], item[1].task_id))
        return tuple(task for _, task in due[: max(limit, 0)])

    def claim_due_provider_retries(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        limit: int = 20,
    ) -> tuple[TripTask, ...]:
        if not worker_id.strip() or lease_duration.total_seconds() <= 0 or limit < 1:
            raise ValueError("worker_id, positive lease_duration, and limit are required")
        from corporate_travel_agent.domain.enums import TaskState

        with self._lock:
            claimed: list[TripTask] = []
            candidates: list[tuple[datetime, TripTask]] = []
            for task in self._tasks.values():
                retry = task.metadata.get("provider_retry")
                if not isinstance(retry, dict):
                    continue
                next_retry = retry.get("next_retry_at")
                next_retry_at = (
                    datetime.fromisoformat(next_retry)
                    if isinstance(next_retry, str)
                    else None
                )
                lease_until = retry.get("lease_until")
                parsed = (
                    datetime.fromisoformat(lease_until)
                    if isinstance(lease_until, str)
                    else None
                )
                if parsed is not None and parsed > now:
                    continue
                is_due_wait = (
                    task.state is TaskState.WAITING_FOR_PROVIDER
                    and retry.get("status") == "scheduled"
                    and next_retry_at is not None
                    and next_retry_at <= now
                )
                is_stale_claim = (
                    task.state in {TaskState.SEARCHING, TaskState.REVALIDATING}
                    and isinstance(retry.get("attempt_token"), str)
                    and parsed is not None
                    and parsed <= now
                )
                if not is_due_wait and not is_stale_claim:
                    continue
                candidates.append((next_retry_at or parsed or now, task))
            candidates.sort(key=lambda item: (item[0], item[1].task_id))
            for _, task in candidates[:limit]:
                retry = task.metadata["provider_retry"]
                token = str(uuid4())
                retry.update(
                    {
                        "lease_owner": worker_id,
                        "lease_until": (now + lease_duration).isoformat(),
                        "attempt_token": token,
                    }
                )
                self._retry_claim_tokens[task.task_id] = token
                claimed.append(task)
            return tuple(claimed)

    def release_provider_retry_claim(self, task: TripTask, *, attempt_token: str) -> bool:
        with self._lock:
            if self._retry_claim_tokens.get(task.task_id) != attempt_token:
                return False
            retry = task.metadata.get("provider_retry")
            if isinstance(retry, dict):
                for key in ("lease_owner", "lease_until", "attempt_token"):
                    retry.pop(key, None)
            self._retry_claim_tokens.pop(task.task_id, None)
            return True

    def record(
        self,
        task: TripTask,
        event: AuditEvent,
        *,
        outbox_events: Sequence[OutboxEventDraft] = (),
        preceding: Sequence[AuditEvent] = (),
    ) -> None:
        if task.task_id not in self._tasks:
            raise NotFoundError(task.task_id)
        events = (*preceding, event)
        task.persistence_revision += len(events)
        self._tasks[task.task_id] = task
        self._events[task.task_id].extend(events)
        self._updated_at[task.task_id] = datetime.now()
        for draft in outbox_events:
            self.outbox.add(
                draft.materialize(aggregate_type="trip_task", aggregate_id=task.task_id)
            )

    def add_with_trip(
        self,
        task: TripTask,
        *,
        trip: Trip,
        trip_is_new: bool,
        trips: TripRepository,
    ) -> None:
        # 内存里没有"半截"可言：先任务后差旅就是这里的全部语义。
        self.add(task)
        if trip_is_new:
            trips.add(trip)
        else:
            trips.save(trip)

    def record_with_trip(
        self,
        task: TripTask,
        event: AuditEvent,
        *,
        trip: Trip,
        trips: TripRepository,
        outbox_events: Sequence[OutboxEventDraft] = (),
        preceding: Sequence[AuditEvent] = (),
    ) -> None:
        self.record(task, event, outbox_events=outbox_events, preceding=preceding)
        trips.save(trip)

    def events(self, task_id: str) -> tuple[AuditEvent, ...]:
        self.get(task_id)
        return tuple(self._events[task_id])

    def add_snapshot(self, task_id: str, snapshot: InventorySnapshot) -> None:
        self.get(task_id)
        existing = self._snapshots[task_id].get(snapshot.snapshot_id)
        if existing is not None and existing != snapshot:
            raise SnapshotConflictError(snapshot.snapshot_id)
        self._snapshots[task_id][snapshot.snapshot_id] = snapshot

    def snapshots(self, task_id: str) -> tuple[InventorySnapshot, ...]:
        self.get(task_id)
        return tuple(self._snapshots[task_id].values())

    def _filtered(
        self,
        *,
        employee_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> tuple[TripTask, ...]:
        items = []
        for task in self._tasks.values():
            if employee_id is not None and task.employee.employee_id != employee_id:
                continue
            if state is not None and task.state.value != state:
                continue
            items.append(task)
        items.sort(
            key=lambda task: (
                self._updated_at.get(task.task_id) or datetime.min,
                task.task_id,
            )
        )
        return tuple(items[: max(limit, 0)])


class InMemoryEmployeeDirectory:
    """内存员工档案目录。"""

    def __init__(self, profiles: list[EmployeeProfileSnapshot]) -> None:
        self._profiles = {item.employee_id: item for item in profiles}

    def snapshot(self, employee_id: str) -> EmployeeProfileSnapshot:
        """按员工 ID 返回档案快照。"""
        try:
            return self._profiles[employee_id]
        except KeyError as exc:
            raise NotFoundError(f"employee:{employee_id}") from exc

    def knows(self, employee_id: str) -> bool:
        return employee_id in self._profiles

    def delegators_of(self, employee_id: str) -> tuple[str, ...]:
        """这位员工可以替谁订差旅（谁把他列成了委托人）。"""
        return tuple(
            sorted(
                item.employee_id
                for item in self._profiles.values()
                if employee_id in item.delegate_ids
            )
        )

    def may_book_for(self, requester_id: str, traveler_id: str) -> bool:
        """发起人能不能替旅行者订：本人，或者在旅行者的委托名单上。"""
        if requester_id == traveler_id:
            return True
        traveler = self._profiles.get(traveler_id)
        return traveler is not None and requester_id in traveler.delegate_ids


class InMemoryPolicyRepository:
    """内存策略快照仓储，支持指定当前激活快照。"""

    def __init__(
        self,
        policy: PolicySnapshot | list[PolicySnapshot] | tuple[PolicySnapshot, ...],
        *,
        current_snapshot_id: str | None = None,
    ) -> None:
        policies = (policy,) if isinstance(policy, PolicySnapshot) else tuple(policy)
        if not policies:
            raise ValueError("At least one policy snapshot is required")
        self._policies = {item.snapshot_id: item for item in policies}
        if len(self._policies) != len(policies):
            raise ValueError("Policy snapshot IDs must be unique")
        self._current_snapshot_id = current_snapshot_id or policies[-1].snapshot_id
        if self._current_snapshot_id not in self._policies:
            raise ValueError("Current policy snapshot is unavailable")

    def current(self) -> PolicySnapshot:
        """返回当前激活的策略快照。"""
        return self._policies[self._current_snapshot_id]

    def snapshot(self, snapshot_id: str) -> PolicySnapshot:
        """按 ID 返回策略快照。"""
        try:
            return self._policies[snapshot_id]
        except KeyError as exc:
            raise NotFoundError(f"policy:{snapshot_id}") from exc
