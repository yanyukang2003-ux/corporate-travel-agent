"""费控记录表：导入的报销记录和它们的对账结果；渠道外预订率从这里算。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from corporate_travel_agent.services.sqlalchemy_repository import JSON_DOCUMENT

revision: str = "0009_expense_records"
down_revision: str | None = "0008_requester_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expense_records",
        sa.Column("expense_id", sa.String(length=96), nullable=False),
        sa.Column("employee_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("expensed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_references", JSON_DOCUMENT, nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("cost_center", sa.String(length=64), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("matched_task_id", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("expense_id"),
    )
    op.create_index(
        "ix_expense_records_employee", "expense_records", ["employee_id", "expensed_at"]
    )
    op.create_index("ix_expense_records_status", "expense_records", ["status", "imported_at"])


def downgrade() -> None:
    op.drop_index("ix_expense_records_status", table_name="expense_records")
    op.drop_index("ix_expense_records_employee", table_name="expense_records")
    op.drop_table("expense_records")
