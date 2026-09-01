"""供应商熔断状态表：多实例共用"打开到几点"，不再各自撞一遍。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_provider_circuit_state"
down_revision: str | None = "0010_trips"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_circuit_state",
        sa.Column("circuit_key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("circuit_key"),
    )


def downgrade() -> None:
    op.drop_table("provider_circuit_state")
