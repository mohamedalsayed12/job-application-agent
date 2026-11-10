"""A vector per job for the search box on the listings page (D48)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_job_embeddings"
down_revision: str | None = "0012_applicant_degrees"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_embedding",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("job.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("vector", postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("job_embedding")
