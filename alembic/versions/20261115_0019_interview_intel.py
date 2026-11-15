"""Interview intel: evidence, shared packs, applicant requests, and spend kinds."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_interview_intel"
down_revision: str | None = "0018_drop_old_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'linkedin_jobs', "
    "'interview_collect', 'ai_interview_analyse', 'ai_interview_plan', 'retired'"
)
_OLD_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'linkedin_jobs', 'retired'"
)


def upgrade() -> None:
    op.create_table(
        "interview_intel_pack",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("role_key", sa.Text(), nullable=False),
        sa.Column("role_label", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("success_themes", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("failure_themes", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("differentiators", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("materials", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("practice_plan", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_amount", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_intel_pack")),
    )
    op.create_index(
        "ix_interview_intel_pack_role_key_expires",
        "interview_intel_pack",
        ["role_key", "expires_at"],
    )

    op.create_table(
        "interview_evidence",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("role_key", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), server_default=sa.text("'unclear'"), nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("methods", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("materials", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("fetch_run_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "platform IN ('reddit', 'youtube', 'linkedin')",
            name="platform",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure', 'unclear')",
            name="outcome",
        ),
        sa.ForeignKeyConstraint(["fetch_run_id"], ["fetch_run.id"], name=op.f("fk_interview_evidence_fetch_run_id_fetch_run")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_evidence")),
        sa.UniqueConstraint("role_key", "content_hash", name="uq_interview_evidence_role_key_content_hash"),
    )
    op.create_index("ix_interview_evidence_role_key", "interview_evidence", ["role_key"])

    op.create_table(
        "interview_intel_request",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("role_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pack_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ready', 'failed')",
            name="status",
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"], ["applicant.id"], name=op.f("fk_interview_intel_request_applicant_id_applicant"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pack_id"], ["interview_intel_pack.id"], name=op.f("fk_interview_intel_request_pack_id_interview_intel_pack"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_intel_request")),
    )
    op.create_index("ix_interview_intel_request_status", "interview_intel_request", ["status"])

    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_OLD_KINDS})")
    op.drop_index("ix_interview_intel_request_status", table_name="interview_intel_request")
    op.drop_table("interview_intel_request")
    op.drop_index("ix_interview_evidence_role_key", table_name="interview_evidence")
    op.drop_table("interview_evidence")
    op.drop_index("ix_interview_intel_pack_role_key_expires", table_name="interview_intel_pack")
    op.drop_table("interview_intel_pack")
