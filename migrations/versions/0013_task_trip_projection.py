"""任务表加"属于哪趟差旅"投影列：差旅按任务反查走索引，不再全表扫描差旅载荷。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_task_trip_projection"
down_revision: str | None = "0012_trip_watch_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trip_tasks", sa.Column("trip_id", sa.String(length=64), nullable=True))
    op.create_index("ix_trip_tasks_trip_id", "trip_tasks", ["trip_id"])
    # 回填：载荷里记了差旅的，投影列也记上；接差旅聚合之前建的旧任务保持 NULL。
    op.execute(
        sa.text(
            "UPDATE trip_tasks SET trip_id = payload->'task'->>'trip_id' "
            "WHERE trip_id IS NULL AND payload->'task'->>'trip_id' IS NOT NULL"
        )
        if op.get_bind().dialect.name == "postgresql"
        else sa.text(
            "UPDATE trip_tasks SET trip_id = json_extract(payload, '$.task.trip_id') "
            "WHERE trip_id IS NULL AND json_extract(payload, '$.task.trip_id') IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_trip_tasks_trip_id", table_name="trip_tasks")
    op.drop_column("trip_tasks", "trip_id")
