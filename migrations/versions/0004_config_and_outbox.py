"""持久化政策配置快照与事务性 outbox 事件。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_config_and_outbox"
down_revision: str | None = "0003_task_query_projections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "policy_configurations",
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column("active_policy_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_identifier", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("config_id"),
        sa.UniqueConstraint("sha256", name="uq_policy_configurations_sha256"),
    )
    op.create_index(
        "ix_policy_configurations_active",
        "policy_configurations",
        ["is_active", "created_at"],
    )

    op.create_table(
        "employee_profiles",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("employee_id", sa.String(length=64), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("department", sa.String(length=100), nullable=False),
        sa.Column("home_city", sa.String(length=100), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["policy_configurations.config_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    op.create_index(
        "ix_employee_profiles_employee_id",
        "employee_profiles",
        ["employee_id"],
        unique=False,
    )

    op.create_table(
        "policy_snapshot_rows",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["policy_configurations.config_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )

    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=96), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["published_at", "created_at"],
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_table("policy_snapshot_rows")
    op.drop_index("ix_employee_profiles_employee_id", table_name="employee_profiles")
    op.drop_table("employee_profiles")
    op.drop_index("ix_policy_configurations_active", table_name="policy_configurations")
    op.drop_table("policy_configurations")
