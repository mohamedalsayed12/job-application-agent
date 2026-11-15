"""Add labelled flag and token counters on interview evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_interview_evidence_labelled"
down_revision: str | None = "0019_interview_intel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "interview_evidence",
        sa.Column("labelled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "interview_evidence",
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "interview_evidence",
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("interview_evidence", "completion_tokens")
    op.drop_column("interview_evidence", "prompt_tokens")
    op.drop_column("interview_evidence", "labelled")
