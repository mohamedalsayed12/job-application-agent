"""Initial empty base revision (schema tables arrive in later steps).

Revision ID: 0001_initial_base
Revises:
Create Date: 2026-11-05

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_initial_base"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Step 0: empty chain so `alembic upgrade head` succeeds.
    # Domain tables are added from Step 1 onward.
    pass


def downgrade() -> None:
    pass
