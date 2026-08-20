"""持久化无凭证的 Provider 报价重验上下文。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_provider_quote_contexts"
down_revision: str | None = "0004_config_and_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "provider_quote_contexts",
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("snapshot_id", sa.String(length=96), nullable=False),
        sa.Column("ref_id", sa.String(length=160), nullable=False),
        sa.Column("price", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expires_at > captured_at",
            name="ck_provider_quote_contexts_valid_window",
        ),
        sa.PrimaryKeyConstraint("provider", "snapshot_id", "ref_id"),
    )
    op.create_index(
        "ix_provider_quote_contexts_lookup",
        "provider_quote_contexts",
        ["provider", "ref_id", "expires_at"],
    )
    op.create_index(
        "ix_provider_quote_contexts_expiry",
        "provider_quote_contexts",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_quote_contexts_expiry",
        table_name="provider_quote_contexts",
    )
    op.drop_index(
        "ix_provider_quote_contexts_lookup",
        table_name="provider_quote_contexts",
    )
    op.drop_table("provider_quote_contexts")
