"""Tailored CVs: a running status for packages in progress, and when the person asked for one."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_tailored_cv_running"
down_revision: str | None = "0026_saved_questions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "'pending', 'waiting_budget', 'ready', 'skipped', 'failed'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tailored_cv_status"), "tailored_cv", type_="check")
    op.create_check_constraint(
        op.f("ck_tailored_cv_status"), "tailored_cv", f"status IN ('running', {_STATUSES})"
    )
    op.add_column("tailored_cv", sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True))
    # Before this column, a row with no Apply/Consider report behind it existed only because someone asked.
    op.execute(
        """
        UPDATE tailored_cv t SET requested_at = t.updated_at
        WHERE t.input_hash = 'requested' OR NOT EXISTS (
            SELECT 1 FROM job_report r
            WHERE r.applicant_id = t.applicant_id AND r.job_id = t.job_id AND r.verdict IN ('Apply', 'Consider')
        )
        """
    )


def downgrade() -> None:
    op.execute("UPDATE tailored_cv SET status = 'pending' WHERE status = 'running'")
    op.drop_column("tailored_cv", "requested_at")
    op.drop_constraint(op.f("ck_tailored_cv_status"), "tailored_cv", type_="check")
    op.create_check_constraint(op.f("ck_tailored_cv_status"), "tailored_cv", f"status IN ({_STATUSES})")
