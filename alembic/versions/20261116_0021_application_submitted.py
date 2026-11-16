"""Applications can be marked submitted by the person."""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_application_submitted"
down_revision: str | None = "0019_apply_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'preparing', 'ready_to_fill', 'filled_awaiting_review', 'needs_input', 'failed', 'abandoned'"
_NEW = "'preparing', 'ready_to_fill', 'filled_awaiting_review', 'submitted', 'needs_input', 'failed', 'abandoned'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_application_status"), "application", type_="check")
    op.create_check_constraint(op.f("ck_application_status"), "application", f"status IN ({_NEW})")


def downgrade() -> None:
    op.execute("UPDATE application SET status = 'filled_awaiting_review' WHERE status = 'submitted'")
    op.drop_constraint(op.f("ck_application_status"), "application", type_="check")
    op.create_check_constraint(op.f("ck_application_status"), "application", f"status IN ({_OLD})")
