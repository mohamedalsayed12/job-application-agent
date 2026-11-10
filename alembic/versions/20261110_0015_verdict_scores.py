"""The AI's overall 1-5 score and its rating per area on each job verdict (D51)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_verdict_scores"
down_revision: str | None = "0014_job_verdicts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_verdict", sa.Column("score", sa.Numeric(2, 1), nullable=True))
    op.add_column("job_verdict", sa.Column("ratings", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_verdict", "ratings")
    op.drop_column("job_verdict", "score")
