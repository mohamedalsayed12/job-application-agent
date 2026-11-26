"""Each fetch run's search key so a role and location is due on its own (D72).

Related titles and ai_related_titles already exist from 0035_related_titles (D68).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_search_keys"
down_revision: str | None = "0036_google_jobs_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fetch_run", sa.Column("search_key", sa.Text(), nullable=True))
    op.create_index("ix_fetch_run_source_search_key", "fetch_run", ["source", "search_key", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_fetch_run_source_search_key", table_name="fetch_run")
    op.drop_column("fetch_run", "search_key")
