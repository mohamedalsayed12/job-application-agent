"""job_posting.places and job.places: every place a job is listed for (D34)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0004_job_places"
down_revision: str | None = "0003_candidate_domain_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("job_posting", "job"):
        op.add_column(
            table,
            sa.Column("places", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        )


def downgrade() -> None:
    for table in ("job_posting", "job"):
        op.drop_column(table, "places")
