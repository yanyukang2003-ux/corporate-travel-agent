"""差旅观察排程：下一次查航班动态的时刻和 worker 租约，让多个 watch worker 靠数据库互斥。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_trip_watch_schedule"
down_revision: str | None = "0011_provider_circuit_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trips", sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trips", sa.Column("watch_lease_owner", sa.String(length=128), nullable=True))
    op.add_column(
        "trips", sa.Column("watch_lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_trips_next_check_at", "trips", ["status", "next_check_at"])


def downgrade() -> None:
    op.drop_index("ix_trips_next_check_at", table_name="trips")
    op.drop_column("trips", "watch_lease_until")
    op.drop_column("trips", "watch_lease_owner")
    op.drop_column("trips", "next_check_at")
