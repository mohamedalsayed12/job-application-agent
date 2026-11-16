"""Company research cache, tailored CV packages, and ai_cv_tailor spend (D55)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019_cv_tailor"
down_revision: str | None = "0018_indeed_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'linkedin_jobs', 'indeed_jobs', 'retired'"
_NEW = _KINDS.replace("'linkedin_jobs'", "'ai_cv_tailor', 'linkedin_jobs'")


def upgrade() -> None:
    op.create_table(
        "company_research",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_key", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("research", postgresql.JSONB(), nullable=False),
        sa.Column("official_domain", sa.Text()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("company_key", name="uq_company_research_company_key"),
        sa.CheckConstraint("status IN ('ready', 'unavailable')", name="status"),
    )
    op.create_table(
        "tailored_cv",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("applicant_id", sa.Uuid(), sa.ForeignKey("applicant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("draft", postgresql.JSONB()),
        sa.Column("keywords", postgresql.JSONB()),
        sa.Column("ats_report", postgresql.JSONB()),
        sa.Column("company_research_id", sa.Uuid(), sa.ForeignKey("company_research.id", ondelete="SET NULL")),
        sa.Column("company_research_available", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pdf_content", sa.LargeBinary()),
        sa.Column("docx_content", sa.LargeBinary()),
        sa.Column("pdf_size_bytes", sa.Integer()),
        sa.Column("docx_size_bytes", sa.Integer()),
        sa.Column("model", sa.Text()),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_amount", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("applicant_id", "job_id", name="uq_tailored_cv_applicant_job"),
        # Constraint names are short; MetaData naming convention prefixes ck_<table>_.
        sa.CheckConstraint(
            "status IN ('pending', 'waiting_budget', 'ready', 'skipped')",
            name="status",
        ),
        sa.CheckConstraint(
            "pdf_size_bytes IS NULL OR (pdf_size_bytes BETWEEN 1 AND 5242880)",
            name="pdf_size_bytes",
        ),
        sa.CheckConstraint(
            "docx_size_bytes IS NULL OR (docx_size_bytes BETWEEN 1 AND 5242880)",
            name="docx_size_bytes",
        ),
    )
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_NEW})")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")
    op.drop_table("tailored_cv")
    op.drop_table("company_research")
