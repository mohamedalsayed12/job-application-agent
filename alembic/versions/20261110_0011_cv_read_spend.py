"""Spending on reading a CV to fill the profile form (D43)"""
from collections.abc import Sequence

from alembic import op

revision: str = "0011_cv_read_spend"
down_revision: str | None = "0010_job_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(
        op.f("ck_spend_ledger_kind"), "spend_ledger", "kind IN ('search', 'ai_extraction', 'ai_summary', 'ai_cv_read')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", "kind IN ('search', 'ai_extraction', 'ai_summary')")
