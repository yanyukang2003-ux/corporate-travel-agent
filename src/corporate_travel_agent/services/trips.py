"""差旅聚合仓储：一趟差旅跨越几个任务，从规划到下单到改期。

`Trip` 是任务之上的一层：第一个规划任务建它，下单确认让它进入 `BOOKED` 并登记观察对象
（`TripWatch`），外部变更事件开出改期任务挂在它下面。原任务的审计一个字不动——
改期是**新任务**，不是改旧任务。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from corporate_travel_agent.domain.enums import TripStatus
from corporate_travel_agent.domain.models import Trip
from corporate_travel_agent.services.db_engine import rowcount
from corporate_travel_agent.services.repositories import ConcurrentUpdateError, NotFoundError
from corporate_travel_agent.services.serialization import deserialize_trip
from corporate_travel_agent.services.sqlalchemy_repository import (
    TaskRow,
    TripRow,
    trip_row_from,
    trip_update_statement,
)

#: 还在被盯着的差旅状态。改期进行中（CHANGE_REQUESTED）的不查：旧票已经在换了。
WATCHED_STATUSES: frozenset[TripStatus] = frozenset({TripStatus.BOOKED, TripStatus.REBOOKED})


class TripRepository(Protocol):
    backend_name: str

    def add(self, trip: Trip) -> None: ...

    def get(self, trip_id: str) -> Trip: ...

    def save(self, trip: Trip) -> None: ...

    def list_by_traveler(self, traveler_id: str, *, limit: int = 100) -> tuple[Trip, ...]: ...

    def list_involving(self, employee_id: str, *, limit: int = 100) -> tuple[Trip, ...]: ...

    def list_all(self, *, limit: int = 100) -> tuple[Trip, ...]: ...

    def find_by_task(self, task_id: str) -> Trip | None: ...

    def list_watching(self, ref_id: str) -> tuple[Trip, ...]: ...

    def claim_due_flight_checks(
        self, *, worker_id: str, now: datetime, lease_duration: timedelta, limit: int = 20
    ) -> tuple[Trip, ...]: ...


def _watched(trip: Trip) -> bool:
    return trip.status in WATCHED_STATUSES and trip.watch is not None


def _check_due(trip: Trip, now: datetime) -> bool:
    """已订、排了检查、到点了。`next_check_at` 为 None 的从没排过或已经查完，不领。"""
    if not _watched(trip):
        return False
    due = trip.watch.next_check_at  # type: ignore[union-attr]
    return due is not None and due <= now


class InMemoryTripRepository:
    backend_name = "memory"

    def __init__(self) -> None:
        self._trips: dict[str, Trip] = {}
        self._lock = RLock()
        #: trip_id → (worker, 租约到期)。保存即释放，和 SQL 版一致。
        self._watch_leases: dict[str, tuple[str, datetime]] = {}

    def add(self, trip: Trip) -> None:
        with self._lock:
            if trip.trip_id in self._trips:
                raise ValueError(f"Trip {trip.trip_id} already exists")
            self._trips[trip.trip_id] = trip

    def get(self, trip_id: str) -> Trip:
        try:
            return self._trips[trip_id]
        except KeyError as exc:
            raise NotFoundError(f"trip:{trip_id}") from exc

    def save(self, trip: Trip) -> None:
        with self._lock:
            if trip.trip_id not in self._trips:
                raise NotFoundError(f"trip:{trip.trip_id}")
            trip.persistence_revision += 1
            self._trips[trip.trip_id] = trip
            self._watch_leases.pop(trip.trip_id, None)

    def list_by_traveler(self, traveler_id: str, *, limit: int = 100) -> tuple[Trip, ...]:
        items = [t for t in self._trips.values() if t.traveler_id == traveler_id]
        return tuple(sorted(items, key=lambda t: t.created_at, reverse=True)[:limit])

    def list_involving(self, employee_id: str, *, limit: int = 100) -> tuple[Trip, ...]:
        items = [
            t for t in self._trips.values() if employee_id in {t.traveler_id, t.requester_id}
        ]
        return tuple(sorted(items, key=lambda t: t.created_at, reverse=True)[:limit])

    def list_all(self, *, limit: int = 100) -> tuple[Trip, ...]:
        items = sorted(self._trips.values(), key=lambda t: t.created_at, reverse=True)
        return tuple(items[:limit])

    def find_by_task(self, task_id: str) -> Trip | None:
        return next((t for t in self._trips.values() if task_id in t.task_ids), None)

    def list_watching(self, ref_id: str) -> tuple[Trip, ...]:
        """哪些已订差旅正盯着这张票。同一班航班可能坐着好几位旅行者——推送只带票号时全部命中。"""
        return tuple(
            t
            for t in sorted(self._trips.values(), key=lambda t: t.created_at)
            if _watched(t) and t.watch.leg(ref_id) is not None  # type: ignore[union-attr]
        )

    def claim_due_flight_checks(
        self, *, worker_id: str, now: datetime, lease_duration: timedelta, limit: int = 20
    ) -> tuple[Trip, ...]:
        """领取到点该查的差旅，写租约；另一个 worker 在租约内领不到同一趟。"""
        if not worker_id.strip() or lease_duration.total_seconds() <= 0 or limit < 1:
            raise ValueError("worker_id, positive lease_duration, and limit are required")
        claimed: list[Trip] = []
        with self._lock:
            due = sorted(
                (t for t in self._trips.values() if _check_due(t, now)),
                key=lambda t: (t.watch.next_check_at, t.trip_id),  # type: ignore[union-attr]
            )
            for trip in due:
                lease = self._watch_leases.get(trip.trip_id)
                if lease is not None and lease[1] > now:
                    continue
                self._watch_leases[trip.trip_id] = (worker_id, now + lease_duration)
                claimed.append(trip)
                if len(claimed) >= limit:
                    break
        return tuple(claimed)


class SQLAlchemyTripRepository:
    backend_name = "sqlalchemy"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def add(self, trip: Trip) -> None:
        with Session(self.engine) as session, session.begin():
            if session.get(TripRow, trip.trip_id) is not None:
                raise ValueError(f"Trip {trip.trip_id} already exists")
            session.add(trip_row_from(trip))

    def get(self, trip_id: str) -> Trip:
        with Session(self.engine) as session:
            row = session.get(TripRow, trip_id)
            if row is None:
                raise NotFoundError(f"trip:{trip_id}")
            return _from_row(row)

    def save(self, trip: Trip) -> None:
        expected = trip.persistence_revision
        trip.persistence_revision = expected + 1
        try:
            with Session(self.engine) as session, session.begin():
                result = session.execute(trip_update_statement(trip, expected_revision=expected))
                if rowcount(result) != 1:
                    raise ConcurrentUpdateError(f"Trip {trip.trip_id} was updated concurrently")
        except Exception:
            trip.persistence_revision = expected
            raise

    def _list(self, statement, limit: int) -> tuple[Trip, ...]:
        with Session(self.engine) as session:
            rows = session.scalars(statement.order_by(TripRow.created_at.desc()).limit(limit))
            return tuple(_from_row(row) for row in rows)

    def list_by_traveler(self, traveler_id: str, *, limit: int = 100) -> tuple[Trip, ...]:
        return self._list(select(TripRow).where(TripRow.traveler_id == traveler_id), limit)

    def list_involving(self, employee_id: str, *, limit: int = 100) -> tuple[Trip, ...]:
        return self._list(
            select(TripRow).where(
                or_(TripRow.traveler_id == employee_id, TripRow.requester_id == employee_id)
            ),
            limit,
        )

    def list_all(self, *, limit: int = 100) -> tuple[Trip, ...]:
        return self._list(select(TripRow), limit)

    def find_by_task(self, task_id: str) -> Trip | None:
        """任务 → 差旅：先查任务表的 `trip_id` 投影列（迁移 0013），旧行没有投影再扫载荷。"""
        with Session(self.engine) as session:
            trip_id = session.execute(
                select(TaskRow.trip_id).where(TaskRow.task_id == task_id)
            ).scalar_one_or_none()
        if trip_id:
            try:
                return self.get(trip_id)
            except NotFoundError:
                return None
        for trip in self.list_all(limit=10_000):
            if task_id in trip.task_ids:
                return trip
        return None

    def list_watching(self, ref_id: str) -> tuple[Trip, ...]:
        statement = select(TripRow).where(
            TripRow.status.in_(tuple(item.value for item in WATCHED_STATUSES))
        )
        return tuple(
            trip
            for trip in self._list(statement, 10_000)
            if trip.watch is not None and trip.watch.leg(ref_id) is not None
        )

    def claim_due_flight_checks(
        self, *, worker_id: str, now: datetime, lease_duration: timedelta, limit: int = 20
    ) -> tuple[Trip, ...]:
        """按投影列领取到点的差旅并写租约；Postgres 上 `SKIP LOCKED`，多 worker 不撞。

        领到的差旅由调用方处理后 `save()`——更新语句同时重算 `next_check_at` 并清租约。
        """
        if not worker_id.strip() or lease_duration.total_seconds() <= 0 or limit < 1:
            raise ValueError("worker_id, positive lease_duration, and limit are required")
        lease_until = now + lease_duration
        claimed: list[Trip] = []
        with Session(self.engine) as session, session.begin():
            statement = (
                select(TripRow)
                .where(
                    TripRow.status.in_(tuple(item.value for item in WATCHED_STATUSES)),
                    TripRow.next_check_at.is_not(None),
                    TripRow.next_check_at <= now,
                    or_(TripRow.watch_lease_until.is_(None), TripRow.watch_lease_until <= now),
                )
                .order_by(TripRow.next_check_at, TripRow.trip_id)
                .limit(limit)
            )
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            for row in session.scalars(statement):
                row.watch_lease_owner = worker_id
                row.watch_lease_until = lease_until
                claimed.append(_from_row(row))
        return tuple(claimed)


def _from_row(row: TripRow) -> Trip:
    """还原聚合；乐观锁版本以 `revision` **列**为准，不信载荷里那一份。

    两处本该一致；万一哪次写入让它们差了一版（`add_with_trip` 修复之前就是），
    以列为准能让下一次保存照常过，而不是平白报"并发更新"。
    """
    trip = deserialize_trip(row.payload)
    trip.persistence_revision = row.revision
    return trip
