"""Matched skills, missing skills and missing licences on each match (D40)"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_match_skills_licences"
down_revision: str | None = "0008_skills_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("matched_skills", "missing_skills", "missing_licences")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "applicant_job_match",
            sa.Column(name, sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("applicant_job_match", name)
