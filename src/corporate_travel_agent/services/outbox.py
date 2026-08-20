"""事务性发件箱：与业务事务同库写入，保证副作用投递至少一次且可追踪。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from corporate_travel_agent.services.sqlalchemy_repository import OutboxEventRow


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """发件箱中的一条待发布/已发布事件。"""

    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    published_at: datetime | None = None
    attempt_count: int = 0
    last_error: str | None = None


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

    def mark_published(self, event_id: str, *, published_at: datetime | None = None) -> None: ...

    def mark_failed(self, event_id: str, error: str) -> None: ...

    def unpublished_count(self) -> int: ...


class InMemoryOutboxStore:
    """内存发件箱实现，用于测试与无持久化演示。"""

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

    def list_unpublished(self, *, limit: int = 50) -> tuple[OutboxEvent, ...]:
        items = [
            event
            for event in self._events.values()
            if event.published_at is None
        ]
        items.sort(key=lambda event: (event.created_at, event.event_id))
        return tuple(items[: max(limit, 0)])

    def mark_published(self, event_id: str, *, published_at: datetime | None = None) -> None:
        event = self._require(event_id)
        self._events[event_id] = OutboxEvent(
            event_id=event.event_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
            published_at=published_at or datetime.now(UTC),
            attempt_count=event.attempt_count,
            last_error=None,
        )

    def mark_failed(self, event_id: str, error: str) -> None:
        event = self._require(event_id)
        self._events[event_id] = OutboxEvent(
            event_id=event.event_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
            published_at=event.published_at,
            attempt_count=event.attempt_count + 1,
            last_error=error[:512],
        )

    def unpublished_count(self) -> int:
        return sum(1 for event in self._events.values() if event.published_at is None)

    def _require(self, event_id: str) -> OutboxEvent:
        try:
            return self._events[event_id]
        except KeyError as exc:
            raise KeyError(event_id) from exc


class SQLAlchemyOutboxStore:
    """基于 SQLAlchemy 的发件箱实现（PostgreSQL 下支持 SKIP LOCKED 拉取）。"""

    backend_name = "sqlalchemy"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.backend_name = f"sqlalchemy:{engine.dialect.name}"

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
        with Session(self.engine) as session, session.begin():
            session.add(
                OutboxEventRow(
                    event_id=event.event_id,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    event_type=event.event_type,
                    payload=event.payload,
                    created_at=event.created_at,
                    published_at=None,
                    attempt_count=0,
                    last_error=None,
                )
            )
        return event

    def list_unpublished(self, *, limit: int = 50) -> tuple[OutboxEvent, ...]:
        with Session(self.engine) as session:
            statement = (
                select(OutboxEventRow)
                .where(OutboxEventRow.published_at.is_(None))
                .order_by(OutboxEventRow.created_at, OutboxEventRow.event_id)
                .limit(max(limit, 0))
            )
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = session.scalars(statement)
            return tuple(_from_row(row) for row in rows)

    def mark_published(self, event_id: str, *, published_at: datetime | None = None) -> None:
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(OutboxEventRow)
                .where(OutboxEventRow.event_id == event_id)
                .values(
                    published_at=published_at or datetime.now(UTC),
                    last_error=None,
                )
            )
            if result.rowcount != 1:
                raise KeyError(event_id)

    def mark_failed(self, event_id: str, error: str) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(OutboxEventRow, event_id)
            if row is None:
                raise KeyError(event_id)
            row.attempt_count += 1
            row.last_error = error[:512]

    def unpublished_count(self) -> int:
        with Session(self.engine) as session:
            value = session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.published_at.is_(None))
            )
            return int(value or 0)


def _from_row(row: OutboxEventRow) -> OutboxEvent:
    return OutboxEvent(
        event_id=row.event_id,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        event_type=row.event_type,
        payload=dict(row.payload),
        created_at=row.created_at,
        published_at=row.published_at,
        attempt_count=row.attempt_count,
        last_error=row.last_error,
    )
