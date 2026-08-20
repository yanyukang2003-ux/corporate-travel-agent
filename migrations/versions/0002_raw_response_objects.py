"""将库存快照关联到不可变的 Provider 原始响应对象。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_raw_response_objects"
down_revision: str | None = "0001_task_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inventory_snapshots",
        sa.Column("raw_response_object_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "inventory_snapshots",
        sa.Column("raw_response_content_type", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "inventory_snapshots",
        sa.Column("raw_response_size", sa.Integer(), nullable=True),
    )
    op.add_column(
        "inventory_snapshots",
        sa.Column("raw_response_retention_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inventory_snapshots",
        sa.Column("raw_response_access_policy", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_inventory_snapshots_raw_response_object_key",
        "inventory_snapshots",
        ["raw_response_object_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inventory_snapshots_raw_response_object_key",
        table_name="inventory_snapshots",
    )
    op.drop_column("inventory_snapshots", "raw_response_access_policy")
    op.drop_column("inventory_snapshots", "raw_response_retention_until")
    op.drop_column("inventory_snapshots", "raw_response_size")
    op.drop_column("inventory_snapshots", "raw_response_content_type")
    op.drop_column("inventory_snapshots", "raw_response_object_key")
