"""candidate.domain_searched_at: CF3 has run for this candidate (Step 10 R2)"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_candidate_domain_search"
down_revision: str | None = "0002_milestone1_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidate",
        sa.Column("domain_searched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate", "domain_searched_at")
