"""Degrees on the applicant profile, to hide jobs that need a subject they didn't study (D45)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_applicant_degrees"
down_revision: str | None = "0011_cv_read_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applicant_profile",
        sa.Column("degrees", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("applicant_profile", "degrees")
