"""为任务增加查询投影字段（员工、经理、Provider 重试等）。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_task_query_projections"
down_revision: str | None = "0002_raw_response_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trip_tasks",
        sa.Column("employee_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("manager_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("policy_snapshot_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("selected_option_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("clarification_rounds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("delayed_retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "trip_tasks",
        sa.Column("payload_schema_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_trip_tasks_employee_updated", "trip_tasks", ["employee_id", "updated_at"])
    op.create_index("ix_trip_tasks_manager_state", "trip_tasks", ["manager_id", "state"])
    op.create_index(
        "ix_trip_tasks_provider_retry_due",
        "trip_tasks",
        ["state", "next_retry_at"],
    )

    # Backfill projections from existing JSON payloads where present.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE trip_tasks
                SET
                  employee_id = payload #>> '{task,employee,employee_id}',
                  manager_id = payload #>> '{task,employee,manager_id}',
                  policy_snapshot_id = payload #>> '{task,policy_snapshot_id}',
                  selected_option_id = payload #>> '{task,selected_option_id}',
                  clarification_rounds = COALESCE(
                    (payload #>> '{task,clarification_rounds}')::int,
                    0
                  ),
                  delayed_retry_count = COALESCE(
                    (payload #>> '{task,metadata,provider_retry,delayed_attempts_completed}')::int,
                    0
                  ),
                  next_retry_at = CASE
                    WHEN payload #>> '{task,metadata,provider_retry,next_retry_at}' IS NULL
                      THEN NULL
                    ELSE (payload #>> '{task,metadata,provider_retry,next_retry_at}')::timestamptz
                  END,
                  payload_schema_version = COALESCE(
                    (payload #>> '{schema_version}')::int,
                    1
                  )
                """
            )
        )
    else:
        # SQLite / other: leave null/default; next write will populate projections.
        pass


def downgrade() -> None:
    op.drop_index("ix_trip_tasks_provider_retry_due", table_name="trip_tasks")
    op.drop_index("ix_trip_tasks_manager_state", table_name="trip_tasks")
    op.drop_index("ix_trip_tasks_employee_updated", table_name="trip_tasks")
    op.drop_column("trip_tasks", "payload_schema_version")
    op.drop_column("trip_tasks", "delayed_retry_count")
    op.drop_column("trip_tasks", "next_retry_at")
    op.drop_column("trip_tasks", "clarification_rounds")
    op.drop_column("trip_tasks", "selected_option_id")
    op.drop_column("trip_tasks", "policy_snapshot_id")
    op.drop_column("trip_tasks", "manager_id")
    op.drop_column("trip_tasks", "employee_id")
