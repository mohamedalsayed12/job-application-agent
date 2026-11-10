"""The AI's verdict on whether a job fits a person, and its spending kind (D50)"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_job_verdicts"
down_revision: str | None = "0013_job_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'search', 'ai_extraction', 'ai_summary', 'ai_cv_read'"


def upgrade() -> None:
    op.create_table(
        "job_verdict",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("applicant_id", sa.Uuid(), sa.ForeignKey("applicant.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fits", sa.Boolean(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("job_hash", sa.Text(), nullable=False),
        sa.Column("profile_hash", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_amount", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("applicant_id", "job_id", name="uq_job_verdict_applicant_job"),
    )
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS}, 'ai_job_judge')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")
    op.drop_table("job_verdict")
