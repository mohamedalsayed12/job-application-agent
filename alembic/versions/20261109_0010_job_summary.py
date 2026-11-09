"""One checked summary per job, written on first view (D41)"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_job_summary"
down_revision: str | None = "0009_match_skills_licences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_summary",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("unsupported", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_amount", sa.Numeric(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name=op.f("ck_job_summary_status")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job.id"], name=op.f("fk_job_summary_job_id_job"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_summary")),
        sa.UniqueConstraint("job_id", name="uq_job_summary_job_id"),
    )


def downgrade() -> None:
    op.drop_table("job_summary")
