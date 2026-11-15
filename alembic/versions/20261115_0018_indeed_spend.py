"""The spending kind for Indeed searches through Apify."""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_indeed_spend"
down_revision: str | None = "0018_drop_old_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'linkedin_jobs', 'retired'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS}, 'indeed_jobs')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")
