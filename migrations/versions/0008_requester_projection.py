"""为任务增加"谁发起的"投影列：助理替高管订的差旅，助理的列表里也要看得见。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_requester_projection"
down_revision: str | None = "0007_pending_approver_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trip_tasks",
        sa.Column("requester_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_trip_tasks_requester_updated",
        "trip_tasks",
        ["requester_id", "updated_at"],
    )
    # 回填：旧任务没记发起人，发起人就是旅行者本人。
    op.execute(
        sa.text("UPDATE trip_tasks SET requester_id = employee_id WHERE requester_id IS NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_trip_tasks_requester_updated", table_name="trip_tasks")
    op.drop_column("trip_tasks", "requester_id")
