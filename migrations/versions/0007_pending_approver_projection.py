"""为任务增加"当前待谁审批"的投影列，让分级审批的第二级审批人也能按索引查到收件箱。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_pending_approver_projection"
down_revision: str | None = "0006_provider_retry_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trip_tasks",
        sa.Column("pending_approver_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_trip_tasks_pending_approver",
        "trip_tasks",
        ["pending_approver_id", "state"],
    )

    # 回填：已经在等审批的任务，当前审批人就是聚合里 approval.approver_id。
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text(
                "UPDATE trip_tasks SET pending_approver_id = "
                "payload->'task'->'approval'->>'approver_id' "
                "WHERE state = 'WAITING_FOR_APPROVAL'"
            )
        )
    elif dialect == "sqlite":
        op.execute(
            sa.text(
                "UPDATE trip_tasks SET pending_approver_id = "
                "json_extract(payload, '$.task.approval.approver_id') "
                "WHERE state = 'WAITING_FOR_APPROVAL'"
            )
        )


def downgrade() -> None:
    op.drop_index("ix_trip_tasks_pending_approver", table_name="trip_tasks")
    op.drop_column("trip_tasks", "pending_approver_id")
