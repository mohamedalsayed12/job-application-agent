"""applicant.password_hash and session_version: accounts that can log in (D37)"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_applicant_login"
down_revision: str | None = "0005_search_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("applicant", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "applicant",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("applicant", "session_version")
    op.drop_column("applicant", "password_hash")
