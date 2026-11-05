"""Initial empty base revision (schema tables arrive in later steps).

Revision ID: 0001_initial_base
Revises:
Create Date: 2026-11-05

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_initial_base"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 0: empty chain so `alembic upgrade head` succeeds.
    # Domain tables are added from Step 1 onward.
    pass


def downgrade() -> None:
    pass
