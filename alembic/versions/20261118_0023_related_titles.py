"""Related job titles, kept once for everyone, and their spend kind"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023_related_titles"
down_revision: str | None = "0022_experience_levels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'ai_cv_tailor', 'linkedin_jobs', "
    "'indeed_jobs', 'apply_form', 'ai_apply_map', 'linkedin_people', 'linkedin_profile', 'ai_outreach', "
    "'interview_collect', 'ai_interview_analyse', 'ai_interview_plan', 'ai_interview_tailor', 'retired'"
)
_KINDS = _OLD_KINDS.replace("'retired'", "'ai_related_titles', 'retired'")


def upgrade() -> None:
    op.create_table(
        "related_title",
        sa.Column("title_key", sa.Text(), nullable=False),
        sa.Column("titles", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("title_key"),
    )
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")


def downgrade() -> None:
    op.execute("UPDATE spend_ledger SET kind = 'retired' WHERE kind = 'ai_related_titles'")
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_OLD_KINDS})")
    op.drop_table("related_title")
