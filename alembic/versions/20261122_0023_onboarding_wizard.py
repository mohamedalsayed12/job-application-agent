"""Sign-up wizard: where each person is in it, CV suggestions waiting for review, and new answers.

Availability and extra UK diversity questions join the employer-application answers. The
diversity answers default to prefer not to say, like the ones already there.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023_onboarding_wizard"
down_revision: str | None = "0022_tailored_cover_letter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREFER_NOT = sa.text("'prefer_not_to_say'")
_STEPS = ("cv", "details", "preferences", "availability", "diversity")
_DIVERSITY = ("sexual_orientation", "religion", "caring_responsibilities", "school_type", "parental_occupation")


def upgrade() -> None:
    # Null means finished, so everyone who already has an account skips the wizard.
    op.add_column("applicant", sa.Column("onboarding_step", sa.Text(), nullable=True))
    literals = ", ".join(f"'{step}'" for step in _STEPS)
    op.create_check_constraint(
        op.f("ck_applicant_onboarding_step"), "applicant", f"onboarding_step IS NULL OR onboarding_step IN ({literals})"
    )
    op.add_column("cv_file", sa.Column("suggestions", postgresql.JSONB(), nullable=True))
    op.add_column("application_answers", sa.Column("notice_period", sa.Text(), nullable=True))
    op.add_column("application_answers", sa.Column("earliest_start_date", sa.Date(), nullable=True))
    op.add_column("application_answers", sa.Column("willing_to_relocate", sa.Boolean(), nullable=True))
    op.add_column("application_answers", sa.Column("work_hours", sa.Text(), nullable=True))
    op.add_column("application_answers", sa.Column("work_location", sa.Text(), nullable=True))
    for column in _DIVERSITY:
        op.add_column(
            "application_answers", sa.Column(column, sa.Text(), nullable=False, server_default=_PREFER_NOT)
        )


def downgrade() -> None:
    for column in _DIVERSITY:
        op.drop_column("application_answers", column)
    for column in ("work_location", "work_hours", "willing_to_relocate", "earliest_start_date", "notice_period"):
        op.drop_column("application_answers", column)
    op.drop_column("cv_file", "suggestions")
    op.drop_constraint(op.f("ck_applicant_onboarding_step"), "applicant", type_="check")
    op.drop_column("applicant", "onboarding_step")
