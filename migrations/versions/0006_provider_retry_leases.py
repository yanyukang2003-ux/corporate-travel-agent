"""为延迟 Provider 重试增加持久化 lease 与 fencing 字段。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_provider_retry_leases"
down_revision: str | None = "0005_provider_quote_contexts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trip_tasks",
        sa.Column("retry_lease_owner", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("retry_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("retry_attempt_token", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_trip_tasks_retry_lease",
        "trip_tasks",
        ["state", "retry_lease_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_trip_tasks_retry_lease", table_name="trip_tasks")
    op.drop_column("trip_tasks", "retry_attempt_token")
    op.drop_column("trip_tasks", "retry_lease_until")
    op.drop_column("trip_tasks", "retry_lease_owner")
