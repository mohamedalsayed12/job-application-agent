"""Allow tailored_cv.status = failed so paid failures are not retried forever"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_tailored_cv_failed"
down_revision: str | None = "0019_cv_tailor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0019 briefly shipped double-prefixed names when CheckConstraint used a full ck_* name.
_OLD_STATUS = (
    "ck_tailored_cv_ck_tailored_cv_status",
    "ck_tailored_cv_status",
)


def upgrade() -> None:
    for name in _OLD_STATUS:
        op.execute(sa.text(f'ALTER TABLE tailored_cv DROP CONSTRAINT IF EXISTS "{name}"'))
    op.create_check_constraint(
        op.f("ck_tailored_cv_status"),
        "tailored_cv",
        "status IN ('pending', 'waiting_budget', 'ready', 'skipped', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_tailored_cv_status"), "tailored_cv", type_="check")
    op.create_check_constraint(
        op.f("ck_tailored_cv_status"),
        "tailored_cv",
        "status IN ('pending', 'waiting_budget', 'ready', 'skipped')",
    )
