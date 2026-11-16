"""Add job-specific interview packs and their spend kind."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_interview_job_pack"
down_revision: str | None = "0020_interview_evidence_labelled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'ai_cv_tailor', 'linkedin_jobs', "
    "'indeed_jobs', 'apply_form', 'ai_apply_map', 'linkedin_people', 'linkedin_profile', 'ai_outreach', "
    "'interview_collect', 'ai_interview_analyse', 'ai_interview_plan', 'retired'"
)
_KINDS = _OLD_KINDS.replace("'retired'", "'ai_interview_tailor', 'retired'")


def upgrade() -> None:
    op.create_table(
        "interview_job_pack",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("role_pack_id", sa.Uuid(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("success_themes", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("failure_themes", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("differentiators", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("materials", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("practice_plan", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_amount", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_pack_id"], ["interview_intel_pack.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_interview_job_pack_applicant_job", "interview_job_pack", ["applicant_id", "job_id"])
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_OLD_KINDS})")
    op.drop_index("ix_interview_job_pack_applicant_job", table_name="interview_job_pack")
    op.drop_table("interview_job_pack")
