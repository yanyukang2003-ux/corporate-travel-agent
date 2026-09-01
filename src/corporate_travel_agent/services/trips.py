"""差旅聚合仓储：一趟差旅跨越几个任务，从规划到下单到改期。

`Trip` 是任务之上的一层：第一个规划任务建它，下单确认让它进入 `BOOKED` 并登记观察对象
（`TripWatch`），外部变更事件开出改期任务挂在它下面。原任务的审计一个字不动——
改期是**新任务**，不是改旧任务。
"""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from corporate_travel_agent.domain.models import Trip
from corporate_travel_agent.services.repositories import ConcurrentUpdateError, NotFoundError
from corporate_travel_agent.services.serialization import deserialize_trip, serialize_trip
from corporate_travel_agent.services.sqlalchemy_repository import TripRow


class TripRepository(Protocol):
    backend_name: str

    def add(self, trip: Trip) -> None: ...

    def get(self, trip_id: str) -> Trip: ...

    def save(self, trip: Trip) -> None: ...

    def list_by_traveler(self, traveler_id: str, *, limit: int = 100) -> tuple[Trip, ...]: ...

    def list_involving(self, employee_id: str, *, limit: int = 100) -> tuple[Trip, ...]: ...

    def list_all(self, *, limit: int = 100) -> tuple[Trip, ...]: ...

    def find_by_task(self, task_id: str) -> Trip | None: ...


class InMemoryTripRepository:
    backend_name = "memory"

    def __init__(self) -> None:
        self._trips: dict[str, Trip] = {}
        self._lock = RLock()

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


class SQLAlchemyTripRepository:
    backend_name = "sqlalchemy"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def add(self, trip: Trip) -> None:
        with Session(self.engine) as session, session.begin():
            if session.get(TripRow, trip.trip_id) is not None:
                raise ValueError(f"Trip {trip.trip_id} already exists")
            session.add(
                TripRow(
                    trip_id=trip.trip_id,
                    traveler_id=trip.traveler_id,
                    requester_id=trip.requester_id,
                    status=trip.status.value,
                    revision=trip.persistence_revision,
                    payload=serialize_trip(trip),
                    created_at=trip.created_at,
                    updated_at=trip.created_at,
                )
            )

    def get(self, trip_id: str) -> Trip:
        with Session(self.engine) as session:
            row = session.get(TripRow, trip_id)
            if row is None:
                raise NotFoundError(f"trip:{trip_id}")
            return deserialize_trip(row.payload)

    def save(self, trip: Trip) -> None:
        expected = trip.persistence_revision
        trip.persistence_revision = expected + 1
        try:
            with Session(self.engine) as session, session.begin():
                result = session.execute(
                    update(TripRow)
                    .where(TripRow.trip_id == trip.trip_id, TripRow.revision == expected)
                    .values(
                        status=trip.status.value,
                        revision=expected + 1,
                        payload=serialize_trip(trip),
                        updated_at=datetime.now(tz=trip.created_at.tzinfo),
                    )
                )
                if result.rowcount != 1:
                    raise ConcurrentUpdateError(f"Trip {trip.trip_id} was updated concurrently")
        except Exception:
            trip.persistence_revision = expected
            raise

    def _list(self, statement, limit: int) -> tuple[Trip, ...]:
        with Session(self.engine) as session:
            rows = session.scalars(statement.order_by(TripRow.created_at.desc()).limit(limit))
            return tuple(deserialize_trip(row.payload) for row in rows)

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
        # 任务 → 差旅的反查走载荷；任务数量级下够用，真要高频再加关联表。
        for trip in self.list_all(limit=10_000):
            if task_id in trip.task_ids:
                return trip
        return None
