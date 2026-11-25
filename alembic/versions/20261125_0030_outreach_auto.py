"""Outreach after Submitted and email lookups (D76)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_outreach_auto"
down_revision: str | None = "0029_form_questions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'ai_cv_tailor', 'linkedin_jobs', "
    "'indeed_jobs', 'google_jobs', 'apply_form', 'ai_apply_map', 'linkedin_people', 'linkedin_profile', "
    "'ai_outreach', 'interview_collect', 'ai_interview_analyse', 'ai_interview_plan', 'ai_interview_tailor', "
    "'careers_search', 'ai_related_titles', 'retired'"
)
_KINDS = _OLD_KINDS.replace("'retired'", "'email_lookup', 'retired'")


def upgrade() -> None:
    op.add_column("application", sa.Column("outreach_state", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_application_outreach_state"),
        "application",
        "outreach_state IS NULL OR outreach_state IN ('queued', 'ready', 'no_contacts')",
    )
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")


def downgrade() -> None:
    op.execute("UPDATE spend_ledger SET kind = 'retired' WHERE kind = 'email_lookup'")
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_OLD_KINDS})")
    op.drop_constraint(op.f("ck_application_outreach_state"), "application", type_="check")
    op.drop_column("application", "outreach_state")
