"""The extra employer questions, an education step in the sign-up wizard, and Census wording (D74)."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038_extra_answers"
down_revision: str | None = "0037_search_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STEPS = "'cv', 'details', 'preferences', 'availability', 'diversity'"
_STEPS = "'cv', 'details', 'preferences', 'education', 'availability', 'diversity'"
# Saved answers moved to the 2021 Census wording: (column, old, new).
_RENAMED = (
    ("gender", "Woman", "Female"),
    ("gender", "Man", "Male"),
    ("sexual_orientation", "Heterosexual or straight", "Straight or heterosexual"),
    ("sexual_orientation", "Another orientation", "Another sexual orientation"),
)


def _rename(column: str, old: str, new: str) -> None:
    op.execute(
        sa.text(f"UPDATE application_answers SET {column} = :new WHERE {column} = :old").bindparams(old=old, new=new)
    )


def upgrade() -> None:
    op.add_column(
        "application_answers",
        sa.Column("extra", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.drop_constraint(op.f("ck_applicant_onboarding_step"), "applicant", type_="check")
    op.create_check_constraint(
        op.f("ck_applicant_onboarding_step"), "applicant", f"onboarding_step IS NULL OR onboarding_step IN ({_STEPS})"
    )
    for column, old, new in _RENAMED:
        _rename(column, old, new)


def downgrade() -> None:
    for column, old, new in _RENAMED:
        _rename(column, new, old)
    op.execute("UPDATE applicant SET onboarding_step = 'availability' WHERE onboarding_step = 'education'")
    op.drop_constraint(op.f("ck_applicant_onboarding_step"), "applicant", type_="check")
    op.create_check_constraint(
        op.f("ck_applicant_onboarding_step"), "applicant", f"onboarding_step IS NULL OR onboarding_step IN ({_OLD_STEPS})"
    )
    op.drop_column("application_answers", "extra")
