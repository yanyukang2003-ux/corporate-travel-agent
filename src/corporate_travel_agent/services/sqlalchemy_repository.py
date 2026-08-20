"""SQLAlchemy 持久化：任务/审计/库存/报价上下文/策略/发件箱表模型与仓储实现。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.domain.models import AuditEvent, InventorySnapshot, TripTask
from corporate_travel_agent.services.db_engine import (
    create_database_engine,
    engine_pool_snapshot,
    read_alembic_version,
)
from corporate_travel_agent.services.provider_quote_context import (
    ProviderQuoteContext,
    ProviderQuoteContextExpiredError,
    ProviderQuoteContextMissingError,
)
from corporate_travel_agent.services.repositories import (
    ConcurrentUpdateError,
    NotFoundError,
    SnapshotConflictError,
)
from corporate_travel_agent.services.serialization import (
    SCHEMA_VERSION,
    deserialize_audit_event,
    deserialize_snapshot,
    deserialize_task,
    serialize_audit_event,
    serialize_snapshot,
    serialize_task,
)
from corporate_travel_agent.services.task_projections import (
    TaskSummary,
    projection_fields,
    summarize_task,
)

JSON_DOCUMENT = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


class TaskRow(Base):
    """差旅任务主表行。"""

    __tablename__ = "trip_tasks"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_trip_tasks_revision"),
        Index("ix_trip_tasks_employee_updated", "employee_id", "updated_at"),
        Index("ix_trip_tasks_manager_state", "manager_id", "state"),
        Index("ix_trip_tasks_provider_retry_due", "state", "next_retry_at"),
        Index("ix_trip_tasks_retry_lease", "state", "retry_lease_until"),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    employee_id: Mapped[str | None] = mapped_column(String(64))
    manager_id: Mapped[str | None] = mapped_column(String(64))
    policy_snapshot_id: Mapped[str | None] = mapped_column(String(128))
    selected_option_id: Mapped[str | None] = mapped_column(String(128))
    clarification_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delayed_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_lease_owner: Mapped[str | None] = mapped_column(String(128))
    retry_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_attempt_token: Mapped[str | None] = mapped_column(String(64))
    payload_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=SCHEMA_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEventRow(Base):
    """审计事件表行。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_task_sequence", "task_id", "sequence", unique=True),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip_tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InventorySnapshotRow(Base):
    """库存快照表行。"""

    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        CheckConstraint(
            "valid_until > captured_at",
            name="ck_inventory_snapshots_valid_window",
        ),
        Index("ix_inventory_snapshots_query_hash", "query_hash"),
        Index(
            "ix_inventory_snapshots_raw_response_object_key",
            "raw_response_object_key",
        ),
    )

    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip_tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    snapshot_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response_object_key: Mapped[str | None] = mapped_column(String(512))
    raw_response_content_type: Mapped[str | None] = mapped_column(String(128))
    raw_response_size: Mapped[int | None] = mapped_column(Integer)
    raw_response_retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    raw_response_access_policy: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderQuoteContextRow(Base):
    """供应商报价再校验上下文字段表行。"""

    __tablename__ = "provider_quote_contexts"
    __table_args__ = (
        CheckConstraint(
            "expires_at > captured_at",
            name="ck_provider_quote_contexts_valid_window",
        ),
        Index(
            "ix_provider_quote_contexts_lookup",
            "provider",
            "ref_id",
            "expires_at",
        ),
        Index("ix_provider_quote_contexts_expiry", "expires_at"),
    )

    provider: Mapped[str] = mapped_column(String(80), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    ref_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PolicyConfigurationRow(Base):
    """企业策略配置文档表行。"""

    __tablename__ = "policy_configurations"
    __table_args__ = (
        Index("ix_policy_configurations_active", "is_active", "created_at"),
    )

    config_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    active_policy_snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_identifier: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmployeeProfileRow(Base):
    """员工档案快照表行。"""

    __tablename__ = "employee_profiles"
    __table_args__ = (Index("ix_employee_profiles_employee_id", "employee_id"),)

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manager_id: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=False)
    home_city: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    config_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("policy_configurations.config_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PolicySnapshotRow(Base):
    """策略快照表行。"""

    __tablename__ = "policy_snapshot_rows"

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    config_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("policy_configurations.config_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEventRow(Base):
    """事务性发件箱事件表行。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_unpublished", "published_at", "created_at"),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(96), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(512))


class SQLAlchemyTaskRepository:
    """面向 PostgreSQL 的事务性任务仓储（亦支持 SQLite 测试）。"""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        if not database_url and engine is None:
            raise ValueError("database_url is required")
        self.engine = create_database_engine(database_url, engine=engine)
        self.backend_name = self.engine.url.get_backend_name()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def check_connection(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(select(1))

    def check_schema(self) -> None:
        required = {
            "trip_tasks",
            "audit_events",
            "inventory_snapshots",
            "provider_quote_contexts",
            "policy_configurations",
            "employee_profiles",
            "policy_snapshot_rows",
            "outbox_events",
        }
        available = set(inspect(self.engine).get_table_names())
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(
                "Database schema is missing tables "
                f"{', '.join(missing)}; run 'alembic upgrade head'"
            )
        task_columns = {item["name"] for item in inspect(self.engine).get_columns("trip_tasks")}
        required_task_columns = {
            "employee_id",
            "manager_id",
            "policy_snapshot_id",
            "selected_option_id",
            "clarification_rounds",
            "next_retry_at",
            "delayed_retry_count",
            "retry_lease_owner",
            "retry_lease_until",
            "retry_attempt_token",
            "payload_schema_version",
        }
        missing_task_columns = sorted(required_task_columns - task_columns)
        if missing_task_columns:
            raise RuntimeError(
                "Database schema is missing trip_tasks columns "
                f"{', '.join(missing_task_columns)}; run 'alembic upgrade head'"
            )
        snapshot_columns = {
            item["name"] for item in inspect(self.engine).get_columns("inventory_snapshots")
        }
        required_snapshot_columns = {
            "raw_response_object_key",
            "raw_response_content_type",
            "raw_response_size",
            "raw_response_retention_until",
            "raw_response_access_policy",
        }
        missing_columns = sorted(required_snapshot_columns - snapshot_columns)
        if missing_columns:
            raise RuntimeError(
                "Database schema is missing inventory snapshot columns "
                f"{', '.join(missing_columns)}; run 'alembic upgrade head'"
            )

    def operational_status(self) -> dict[str, Any]:
        self.check_connection()
        return {
            "backend": self.backend_name,
            "alembic_version": read_alembic_version(self.engine),
            "pool": engine_pool_snapshot(self.engine),
        }

    def dispose(self) -> None:
        self.engine.dispose()

    def add(self, task: TripTask) -> None:
        task.persistence_revision = 0
        now = datetime.now(UTC)
        fields = projection_fields(task)
        row = TaskRow(
            task_id=task.task_id,
            state=task.state.value,
            revision=0,
            payload=serialize_task(task),
            created_at=now,
            updated_at=now,
            **fields,
        )
        try:
            with Session(self.engine) as session, session.begin():
                session.add(row)
        except IntegrityError as exc:
            raise ValueError(f"Task {task.task_id} already exists") from exc

    def get(self, task_id: str) -> TripTask:
        with Session(self.engine) as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise NotFoundError(task_id)
            return deserialize_task(row.payload)

    def list_tasks(self) -> tuple[TripTask, ...]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(TaskRow).order_by(TaskRow.created_at, TaskRow.task_id)
            )
            return tuple(deserialize_task(row.payload) for row in rows)

    def list_task_summaries(
        self,
        *,
        employee_id: str | None = None,
        manager_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> tuple[TaskSummary, ...]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        with Session(self.engine) as session:
            statement = select(TaskRow)
            if employee_id is not None:
                statement = statement.where(TaskRow.employee_id == employee_id)
            if manager_id is not None:
                statement = statement.where(TaskRow.manager_id == manager_id)
            if state is not None:
                statement = statement.where(TaskRow.state == state)
            statement = statement.order_by(TaskRow.updated_at.desc(), TaskRow.task_id).limit(
                limit
            )
            rows = session.scalars(statement)
            return tuple(self._summary_from_row(row) for row in rows)

    def list_by_employee(self, employee_id: str, *, limit: int = 100) -> tuple[TripTask, ...]:
        return self._list_filtered(employee_id=employee_id, limit=limit)

    def list_by_state(self, state: str, *, limit: int = 100) -> tuple[TripTask, ...]:
        return self._list_filtered(state=state, limit=limit)

    def list_due_provider_retries(
        self,
        *,
        now: datetime,
        limit: int = 20,
    ) -> tuple[TripTask, ...]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with Session(self.engine) as session:
            statement = (
                select(TaskRow)
                .where(
                    TaskRow.state == TaskState.WAITING_FOR_PROVIDER.value,
                    TaskRow.next_retry_at.is_not(None),
                    TaskRow.next_retry_at <= now,
                )
                .order_by(TaskRow.next_retry_at, TaskRow.task_id)
                .limit(limit)
            )
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = list(session.scalars(statement))
            return tuple(deserialize_task(row.payload) for row in rows)

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
        lease_until = now + lease_duration
        claimed: list[TripTask] = []
        with Session(self.engine) as session, session.begin():
            statement = (
                select(TaskRow)
                .where(
                    or_(
                        (
                            (TaskRow.state == TaskState.WAITING_FOR_PROVIDER.value)
                            & TaskRow.next_retry_at.is_not(None)
                            & (TaskRow.next_retry_at <= now)
                        ),
                        (
                            TaskRow.state.in_(
                                (
                                    TaskState.SEARCHING.value,
                                    TaskState.REVALIDATING.value,
                                )
                            )
                            & TaskRow.retry_attempt_token.is_not(None)
                            & (TaskRow.retry_lease_until <= now)
                        ),
                    ),
                    or_(
                        TaskRow.retry_lease_until.is_(None),
                        TaskRow.retry_lease_until <= now,
                    ),
                )
                .order_by(TaskRow.next_retry_at, TaskRow.retry_lease_until, TaskRow.task_id)
                .limit(limit)
            )
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = list(session.scalars(statement))
            for row in rows:
                token = str(uuid4())
                row.retry_lease_owner = worker_id
                row.retry_lease_until = lease_until
                row.retry_attempt_token = token
                task = deserialize_task(row.payload)
                retry = task.metadata.get("provider_retry")
                if not isinstance(retry, dict):
                    continue
                retry.update(
                    {
                        "lease_owner": worker_id,
                        "lease_until": lease_until.isoformat(),
                        "attempt_token": token,
                    }
                )
                claimed.append(task)
        return tuple(claimed)

    def release_provider_retry_claim(self, task: TripTask, *, attempt_token: str) -> bool:
        retry = task.metadata.get("provider_retry")
        if isinstance(retry, dict):
            for key in ("lease_owner", "lease_until", "attempt_token"):
                retry.pop(key, None)
        with Session(self.engine) as session, session.begin():
            result = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.task_id == task.task_id,
                    TaskRow.retry_attempt_token == attempt_token,
                )
                .values(
                    payload=serialize_task(task),
                    retry_lease_owner=None,
                    retry_lease_until=None,
                    retry_attempt_token=None,
                    updated_at=datetime.now(UTC),
                )
            )
            return result.rowcount == 1

    def record(self, task: TripTask, event: AuditEvent) -> None:
        expected_revision = task.persistence_revision
        next_revision = expected_revision + 1
        task.persistence_revision = next_revision
        fields = projection_fields(task)
        retry = task.metadata.get("provider_retry")
        attempt_token = retry.get("attempt_token") if isinstance(retry, dict) else None
        try:
            with Session(self.engine) as session, session.begin():
                statement = update(TaskRow).where(
                        TaskRow.task_id == task.task_id,
                        TaskRow.revision == expected_revision,
                    )
                if isinstance(attempt_token, str) and attempt_token:
                    statement = statement.where(
                        TaskRow.retry_attempt_token == attempt_token
                    )
                result = session.execute(
                    statement.values(
                        state=task.state.value,
                        revision=next_revision,
                        payload=serialize_task(task),
                        updated_at=datetime.now(UTC),
                        **fields,
                    )
                )
                if result.rowcount != 1:
                    raise ConcurrentUpdateError(
                        f"Task {task.task_id} was updated concurrently"
                    )
                session.add(
                    AuditEventRow(
                        event_id=event.event_id,
                        task_id=event.task_id,
                        sequence=next_revision,
                        event_type=event.event_type,
                        payload=serialize_audit_event(event),
                        created_at=event.created_at,
                    )
                )
        except Exception:
            task.persistence_revision = expected_revision
            raise

    def events(self, task_id: str) -> tuple[AuditEvent, ...]:
        with Session(self.engine) as session:
            if session.get(TaskRow, task_id) is None:
                raise NotFoundError(task_id)
            rows = session.scalars(
                select(AuditEventRow)
                .where(AuditEventRow.task_id == task_id)
                .order_by(AuditEventRow.sequence)
            )
            return tuple(deserialize_audit_event(row.payload) for row in rows)

    def add_snapshot(self, task_id: str, snapshot: InventorySnapshot) -> None:
        payload = serialize_snapshot(snapshot)
        with Session(self.engine) as session, session.begin():
            if session.get(TaskRow, task_id) is None:
                raise NotFoundError(task_id)
            identity = {"task_id": task_id, "snapshot_id": snapshot.snapshot_id}
            existing = session.get(InventorySnapshotRow, identity)
            if existing is not None:
                if existing.payload != payload:
                    raise SnapshotConflictError(snapshot.snapshot_id)
                return
            session.add(
                InventorySnapshotRow(
                    task_id=task_id,
                    snapshot_id=snapshot.snapshot_id,
                    provider=snapshot.provider,
                    query_hash=snapshot.query_hash,
                    raw_payload_hash=snapshot.raw_payload_hash,
                    raw_response_object_key=(
                        snapshot.raw_response.object_key if snapshot.raw_response else None
                    ),
                    raw_response_content_type=(
                        snapshot.raw_response.content_type if snapshot.raw_response else None
                    ),
                    raw_response_size=(
                        snapshot.raw_response.size_bytes if snapshot.raw_response else None
                    ),
                    raw_response_retention_until=(
                        snapshot.raw_response.retention_until
                        if snapshot.raw_response
                        else None
                    ),
                    raw_response_access_policy=(
                        snapshot.raw_response.access_policy.value
                        if snapshot.raw_response
                        else None
                    ),
                    payload=payload,
                    captured_at=snapshot.captured_at,
                    valid_until=snapshot.valid_until,
                )
            )

    def snapshots(self, task_id: str) -> tuple[InventorySnapshot, ...]:
        with Session(self.engine) as session:
            if session.get(TaskRow, task_id) is None:
                raise NotFoundError(task_id)
            rows = session.scalars(
                select(InventorySnapshotRow)
                .where(InventorySnapshotRow.task_id == task_id)
                .order_by(
                    InventorySnapshotRow.captured_at,
                    InventorySnapshotRow.snapshot_id,
                )
            )
            return tuple(deserialize_snapshot(row.payload) for row in rows)

    def _list_filtered(
        self,
        *,
        employee_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> tuple[TripTask, ...]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        with Session(self.engine) as session:
            statement = select(TaskRow)
            if employee_id is not None:
                statement = statement.where(TaskRow.employee_id == employee_id)
            if state is not None:
                statement = statement.where(TaskRow.state == state)
            statement = statement.order_by(TaskRow.updated_at.desc(), TaskRow.task_id).limit(
                limit
            )
            rows = session.scalars(statement)
            return tuple(deserialize_task(row.payload) for row in rows)

    @staticmethod
    def _summary_from_row(row: TaskRow) -> TaskSummary:
        # Prefer projection columns; fall back to payload when backfill is incomplete.
        if row.employee_id and row.manager_id and row.policy_snapshot_id:
            return TaskSummary(
                task_id=row.task_id,
                state=row.state,
                employee_id=row.employee_id,
                manager_id=row.manager_id,
                policy_snapshot_id=row.policy_snapshot_id,
                selected_option_id=row.selected_option_id,
                clarification_rounds=row.clarification_rounds,
                next_retry_at=row.next_retry_at,
                delayed_retry_count=row.delayed_retry_count,
                payload_schema_version=row.payload_schema_version,
                revision=row.revision,
                created_at=row.created_at,
                updated_at=row.updated_at,
                option_count=_option_count(row.payload),
                request_version=_request_version(row.payload),
                failure=_failure(row.payload),
            )
        task = deserialize_task(row.payload)
        return summarize_task(task, created_at=row.created_at, updated_at=row.updated_at)


class SQLAlchemyProviderQuoteContextStore:
    """与应用共用 SQL Engine 的持久化报价上下文适配器。"""

    backend_name = "sqlalchemy"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def put_many(self, contexts: tuple[ProviderQuoteContext, ...]) -> None:
        if len({item.key for item in contexts}) != len(contexts):
            raise ValueError("quote context batch contains duplicate keys")
        now = datetime.now(UTC)
        with Session(self.engine) as session, session.begin():
            if contexts:
                cutoff = max(item.captured_at for item in contexts)
                expired_rows = list(
                    session.scalars(
                        select(ProviderQuoteContextRow)
                        .where(ProviderQuoteContextRow.expires_at <= cutoff)
                        .order_by(ProviderQuoteContextRow.expires_at)
                        .limit(1_000)
                    )
                )
                for row in expired_rows:
                    session.delete(row)
            for context in contexts:
                session.merge(
                    ProviderQuoteContextRow(
                        provider=context.provider,
                        snapshot_id=context.snapshot_id,
                        ref_id=context.ref_id,
                        price=context.price,
                        currency=context.currency,
                        payload=context.payload,
                        captured_at=context.captured_at,
                        expires_at=context.expires_at,
                        updated_at=now,
                    )
                )

    def get(
        self,
        provider: str,
        ref_id: str,
        *,
        observed_at: datetime,
    ) -> ProviderQuoteContext:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        with Session(self.engine) as session:
            row = session.scalar(
                select(ProviderQuoteContextRow)
                .where(
                    ProviderQuoteContextRow.provider == provider,
                    ProviderQuoteContextRow.ref_id == ref_id,
                )
                .order_by(
                    ProviderQuoteContextRow.expires_at.desc(),
                    ProviderQuoteContextRow.captured_at.desc(),
                    ProviderQuoteContextRow.snapshot_id.desc(),
                )
                .limit(1)
            )
            if row is None:
                raise ProviderQuoteContextMissingError(provider, ref_id)
            expires_at = _database_aware(row.expires_at)
            if expires_at <= observed_at:
                raise ProviderQuoteContextExpiredError(
                    provider,
                    ref_id,
                    expires_at=expires_at,
                )
            return ProviderQuoteContext(
                provider=row.provider,
                snapshot_id=row.snapshot_id,
                ref_id=row.ref_id,
                price=row.price,
                currency=row.currency,
                payload=row.payload,
                captured_at=_database_aware(row.captured_at),
                expires_at=expires_at,
            )

    def purge_expired(self, *, before: datetime, limit: int = 1_000) -> int:
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with Session(self.engine) as session, session.begin():
            rows = list(
                session.scalars(
                    select(ProviderQuoteContextRow)
                    .where(ProviderQuoteContextRow.expires_at <= before)
                    .order_by(
                        ProviderQuoteContextRow.expires_at,
                        ProviderQuoteContextRow.provider,
                        ProviderQuoteContextRow.ref_id,
                    )
                    .limit(limit)
                )
            )
            for row in rows:
                session.delete(row)
            return len(rows)


def _database_aware(value: datetime) -> datetime:
    """SQLite drops timezone offsets even for timezone=True columns."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _option_count(payload: dict[str, Any]) -> int:
    task = payload.get("task")
    if not isinstance(task, dict):
        return 0
    options = task.get("options")
    return len(options) if isinstance(options, list) else 0


def _request_version(payload: dict[str, Any]) -> int | None:
    task = payload.get("task")
    if not isinstance(task, dict):
        return None
    request = task.get("request")
    if not isinstance(request, dict):
        return None
    version = request.get("version")
    return version if isinstance(version, int) else None


def _failure(payload: dict[str, Any]) -> str | None:
    task = payload.get("task")
    if not isinstance(task, dict):
        return None
    failure = task.get("failure")
    return failure if isinstance(failure, str) else None
