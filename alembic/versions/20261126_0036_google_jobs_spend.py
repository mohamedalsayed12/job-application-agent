"""The spending kind for Google Jobs searches through SerpAPI (D73)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0036_google_jobs_spend"
down_revision: str | None = "0035_related_titles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'ai_cv_tailor', 'linkedin_jobs', "
    "'indeed_jobs', 'apply_form', 'ai_apply_map', 'linkedin_people', 'linkedin_profile', 'ai_outreach', "
    "'interview_collect', 'ai_interview_analyse', 'ai_interview_plan', 'ai_interview_tailor', "
    "'careers_search', 'ai_search_ideas', 'ai_related_titles', 'retired'"
)
_KINDS = _OLD_KINDS.replace("'indeed_jobs'", "'indeed_jobs', 'google_jobs'")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")


def downgrade() -> None:
    op.execute("UPDATE spend_ledger SET kind = 'retired' WHERE kind = 'google_jobs'")
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_OLD_KINDS})")
