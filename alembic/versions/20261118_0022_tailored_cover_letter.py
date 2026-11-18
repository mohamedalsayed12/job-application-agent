"""Tailored packages carry a cover letter written from the tailored CV."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_tailored_cover_letter"
down_revision: str | None = "0021_interview_job_pack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tailored_cv", sa.Column("cover_letter", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tailored_cv", "cover_letter")
