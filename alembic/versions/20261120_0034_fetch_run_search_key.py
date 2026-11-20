"""Record which search each fetch run was for, so a cycle stopped by the budget can resume."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_fetch_run_search_key"
down_revision: str | None = "0033_job_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fetch_run", sa.Column("search_key", sa.Text(), nullable=True))
    op.create_index("ix_fetch_run_source_started", "fetch_run", ["source", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_fetch_run_source_started", table_name="fetch_run")
    op.drop_column("fetch_run", "search_key")
