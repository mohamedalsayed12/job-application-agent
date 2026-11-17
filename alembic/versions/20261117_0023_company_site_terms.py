"""Cache wording read from an employer's own website."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_company_site_terms"
down_revision: str | None = "0022_tailored_cv_running"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("company_research", sa.Column("site_terms", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("company_research", "site_terms")
