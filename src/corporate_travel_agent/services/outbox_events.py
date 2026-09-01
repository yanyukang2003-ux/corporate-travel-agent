"""发件箱事件的值对象。单独成模块，是为了让任务仓储和发件箱存储都能引用它而不互相 import。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """发件箱中的一条待发布/已发布事件。

    `aggregate_type` / `aggregate_id` 说明它属于哪个聚合（今天只有 ``trip_task``）；
    `event_type` 决定投递给哪个通道；`payload` 只放 ID、金额和状态，不放正文原文、
    不放凭证——它会离开本系统。
    """

    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    published_at: datetime | None = None
    attempt_count: int = 0
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxEventDraft:
    """还没进发件箱的事件：编排器在改任务状态的**同一笔事务**里把它交给仓储。"""

    event_type: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid4()))

    def materialize(
        self, *, aggregate_type: str, aggregate_id: str, created_at: datetime | None = None
    ) -> OutboxEvent:
        return OutboxEvent(
            event_id=self.event_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=self.event_type,
            payload=dict(self.payload),
            created_at=created_at or datetime.now(UTC),
        )


class OutboxStore(Protocol):
    """发件箱存储端口：入队、拉取未发布、标记成功/失败。"""

    backend_name: str

    def enqueue(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> OutboxEvent: ...

    def list_unpublished(self, *, limit: int = 50) -> tuple[OutboxEvent, ...]: ...

    def list_recent(self, *, limit: int = 50) -> tuple[OutboxEvent, ...]: ...

    def mark_published(self, event_id: str, *, published_at: datetime | None = None) -> None: ...

    def mark_failed(self, event_id: str, error: str) -> None: ...

    def unpublished_count(self) -> int: ...


class InMemoryOutboxStore:
    """内存发件箱实现，用于测试与无持久化演示。

    内存任务仓储在 `record()` 里往这里写：和任务更新是同一个进程内的同一步，
    这是内存版能给出的"同一笔事务"。
    """

    backend_name = "memory"

    def __init__(self) -> None:
        self._events: dict[str, OutboxEvent] = {}

    def enqueue(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> OutboxEvent:
        event = OutboxEvent(
            event_id=event_id or str(uuid4()),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=dict(payload),
            created_at=created_at or datetime.now(UTC),
        )
        if event.event_id in self._events:
            raise ValueError(f"Outbox event {event.event_id} already exists")
        self._events[event.event_id] = event
        return event

    def add(self, event: OutboxEvent) -> None:
        if event.event_id in self._events:
            raise ValueError(f"Outbox event {event.event_id} already exists")
        self._events[event.event_id] = event

    def list_unpublished(self, *, limit: int = 50) -> tuple[OutboxEvent, ...]:
        items = [event for event in self._events.values() if event.published_at is None]
        items.sort(key=lambda event: (event.created_at, event.event_id))
        return tuple(items[: max(limit, 0)])

    def list_recent(self, *, limit: int = 50) -> tuple[OutboxEvent, ...]:
        items = sorted(
            self._events.values(),
            key=lambda event: (event.created_at, event.event_id),
            reverse=True,
        )
        return tuple(items[: max(limit, 0)])

    def mark_published(self, event_id: str, *, published_at: datetime | None = None) -> None:
        event = self._require(event_id)
        self._events[event_id] = replace(
            event, published_at=published_at or datetime.now(UTC), last_error=None
        )

    def mark_failed(self, event_id: str, error: str) -> None:
        event = self._require(event_id)
        self._events[event_id] = replace(
            event, attempt_count=event.attempt_count + 1, last_error=error[:512]
        )

    def unpublished_count(self) -> int:
        return sum(1 for event in self._events.values() if event.published_at is None)

    def _require(self, event_id: str) -> OutboxEvent:
        try:
            return self._events[event_id]
        except KeyError as exc:
            raise KeyError(event_id) from exc
