"""Let a person choose whether automatic tailoring covers jobs outside their locations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_tailor_outside_locations"
down_revision: str | None = "0023_company_site_terms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "applicant_profile",
        sa.Column("tailor_outside_locations", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("applicant_profile", "tailor_outside_locations")
