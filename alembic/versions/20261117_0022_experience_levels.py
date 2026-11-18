"""Several experience levels on a profile, in place of one (D63)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022_experience_levels"
down_revision: str | None = "0021_interview_job_pack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVELS = "'internship', 'entry', 'mid', 'senior'"


def upgrade() -> None:
    op.add_column("applicant_profile", sa.Column("experience_levels", postgresql.ARRAY(sa.Text()), nullable=True))
    op.execute("UPDATE applicant_profile SET experience_levels = ARRAY[experience_level]")
    op.alter_column("applicant_profile", "experience_levels", nullable=False)
    op.create_check_constraint(
        op.f("ck_applicant_profile_experience_levels"),
        "applicant_profile",
        f"cardinality(experience_levels) > 0 AND experience_levels <@ ARRAY[{_LEVELS}]::text[]",
    )
    op.drop_column("applicant_profile", "experience_level")


def downgrade() -> None:
    op.add_column("applicant_profile", sa.Column("experience_level", sa.Text(), nullable=True))
    # One level fits the old column: the lowest ordinary level, or internship when that is all they chose.
    op.execute(
        """
        UPDATE applicant_profile SET experience_level = CASE
            WHEN 'entry' = ANY(experience_levels) THEN 'entry'
            WHEN 'mid' = ANY(experience_levels) THEN 'mid'
            WHEN 'senior' = ANY(experience_levels) THEN 'senior'
            ELSE 'internship' END
        """
    )
    op.alter_column("applicant_profile", "experience_level", nullable=False)
    op.create_check_constraint(
        op.f("ck_applicant_profile_experience_level"), "applicant_profile", f"experience_level IN ({_LEVELS})"
    )
    op.drop_column("applicant_profile", "experience_levels")
