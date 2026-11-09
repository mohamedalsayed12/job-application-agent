"""search_run outcomes, and fractional template yields because a shared board is split (MVP section 8)"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_search_outcomes"
down_revision: str | None = "0004_job_places"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_YIELDS = ("new_candidates", "new_verified_boards", "relevant_jobs")


def upgrade() -> None:
    for column in _YIELDS:
        op.alter_column("template_stats", column, type_=sa.Numeric(), existing_nullable=False)
    for column in (*_YIELDS, "freshness_share"):
        op.add_column("search_run", sa.Column(column, sa.Numeric(), nullable=True))
    op.add_column("search_run", sa.Column("outcomes_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for column in (*_YIELDS, "freshness_share", "outcomes_at"):
        op.drop_column("search_run", column)
    for column in _YIELDS:
        op.alter_column(
            "template_stats",
            column,
            type_=sa.Integer(),
            existing_nullable=False,
            postgresql_using=f"round({column})::integer",
        )
