"""Throttle counters live in Postgres so restarts and extra web processes cannot reset them."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_rate_limit_event"
down_revision: str | None = "0022_tailored_cover_letter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_limit_event")),
    )
    op.create_index("ix_rate_limit_event_key_created_at", "rate_limit_event", ["key", "created_at"])
    op.create_index("ix_rate_limit_event_created_at", "rate_limit_event", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_event_created_at", table_name="rate_limit_event")
    op.drop_index("ix_rate_limit_event_key_created_at", table_name="rate_limit_event")
    op.drop_table("rate_limit_event")
