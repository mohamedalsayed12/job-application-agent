"""Drop the tables and columns that served the search-API and job-board sources (D54)"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_drop_old_sources"
down_revision: str | None = "0017_linkedin_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'linkedin_jobs'"
_OLD_KINDS = "'search', 'ai_extraction', " + _KINDS
_TABLES = (
    "audit_sample",
    "review_item",
    "verification_check",
    "candidate_evidence",
    "candidate",
    "search_result",
    "template_stats",
    "search_run",
    "search_template",
    "ai_extraction",
    "employer_board",
    "employer",
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.execute("UPDATE spend_ledger SET kind = 'retired' WHERE kind IN ('search', 'ai_extraction')")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS}, 'retired')")
    op.drop_column("spend_ledger", "related_run_id")
    op.drop_column("fetch_run", "employer_board_id")
    op.drop_column("fetch_run", "search_run_id")
    op.drop_column("job_posting", "employer_board_id")
    op.drop_column("job_posting", "employer_id")
    op.drop_column("job", "employer_id")
    for table in _TABLES:
        op.execute(sa.text(f"DROP TABLE IF EXISTS {table} CASCADE"))


def downgrade() -> None:
    raise NotImplementedError("the dropped source tables are not restored")
