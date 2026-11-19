"""Fake-listing check results per master job (D55)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022_job_verification"
down_revision: str | None = "0021_interview_job_pack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_verification",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("company_number", sa.Text(), nullable=True),
        sa.Column("company_status", sa.Text(), nullable=True),
        sa.Column("official_domain", sa.Text(), nullable=True),
        sa.Column("listing_found", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("company_apply_url", sa.Text(), nullable=True),
        sa.Column("reasons", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('real', 'fake', 'inconclusive')",
            name=op.f("ck_job_verification_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job.id"], name=op.f("fk_job_verification_job_id_job"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_verification")),
        sa.UniqueConstraint("job_id", name="uq_job_verification_job_id"),
    )


def downgrade() -> None:
    op.drop_table("job_verification")
