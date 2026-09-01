"""差旅聚合表：一趟差旅跨越几个任务（规划、改期），原任务不动。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corporate_travel_agent.services.sqlalchemy_repository import JSON_DOCUMENT

revision: str = "0010_trips"
down_revision: str | None = "0009_expense_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trips",
        sa.Column("trip_id", sa.String(length=64), nullable=False),
        sa.Column("traveler_id", sa.String(length=64), nullable=False),
        sa.Column("requester_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("trip_id"),
    )
    op.create_index("ix_trips_traveler_id", "trips", ["traveler_id"])
    op.create_index("ix_trips_requester_id", "trips", ["requester_id"])
    op.create_index("ix_trips_status", "trips", ["status"])


def downgrade() -> None:
    op.drop_index("ix_trips_status", table_name="trips")
    op.drop_index("ix_trips_requester_id", table_name="trips")
    op.drop_index("ix_trips_traveler_id", table_name="trips")
    op.drop_table("trips")
