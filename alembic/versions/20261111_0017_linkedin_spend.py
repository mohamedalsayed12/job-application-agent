"""The spending kind for LinkedIn searches through Apify (D54)"""
from collections.abc import Sequence

from alembic import op

revision: str = "0017_linkedin_spend"
down_revision: str | None = "0016_job_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'search', 'ai_extraction', 'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS}, 'linkedin_jobs')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")
