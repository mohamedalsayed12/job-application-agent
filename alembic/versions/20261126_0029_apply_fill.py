"""Applications read the form from the open page: drop the ATS kind and the preparing status."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_apply_fill"
down_revision: str | None = "0028_multi_cv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'preparing', 'ready_to_fill', 'filled_awaiting_review', 'submitted', 'needs_input', 'failed', 'abandoned'"
_NEW = "'ready_to_fill', 'filled_awaiting_review', 'submitted', 'needs_input', 'failed', 'abandoned'"


def upgrade() -> None:
    op.execute("UPDATE application SET status = 'ready_to_fill' WHERE status = 'preparing'")
    op.drop_constraint(op.f("ck_application_status"), "application", type_="check")
    op.create_check_constraint(op.f("ck_application_status"), "application", f"status IN ({_NEW})")
    op.drop_constraint(op.f("ck_application_ats_kind"), "application", type_="check")
    op.drop_column("application", "ats_kind")


def downgrade() -> None:
    op.add_column("application", sa.Column("ats_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_application_ats_kind"),
        "application",
        "ats_kind IS NULL OR ats_kind IN ('greenhouse', 'lever', 'unknown')",
    )
    op.drop_constraint(op.f("ck_application_status"), "application", type_="check")
    op.create_check_constraint(op.f("ck_application_status"), "application", f"status IN ({_OLD})")
