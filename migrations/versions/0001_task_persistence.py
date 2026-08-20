"""创建任务、审计事件与库存快照表（任务持久化基础）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_task_persistence"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "trip_tasks",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_trip_tasks_revision"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_trip_tasks_state", "trip_tasks", ["state"])

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["trip_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index(
        "ix_audit_events_task_sequence",
        "audit_events",
        ["task_id", "sequence"],
        unique=True,
    )

    op.create_table(
        "inventory_snapshots",
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "valid_until > captured_at",
            name="ck_inventory_snapshots_valid_window",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["trip_tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "snapshot_id"),
    )
    op.create_index(
        "ix_inventory_snapshots_query_hash",
        "inventory_snapshots",
        ["query_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_snapshots_query_hash", table_name="inventory_snapshots")
    op.drop_table("inventory_snapshots")
    op.drop_index("ix_audit_events_task_sequence", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_trip_tasks_state", table_name="trip_tasks")
    op.drop_table("trip_tasks")
