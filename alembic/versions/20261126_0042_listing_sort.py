"""Remember each person's listing sort choice."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_listing_sort"
down_revision: str | None = "0041_interview_job_wait"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applicant_preference",
        sa.Column("listing_sort", sa.Text(), server_default=sa.text("'newest'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("applicant_preference", "listing_sort")
