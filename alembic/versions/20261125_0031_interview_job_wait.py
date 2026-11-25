"""The job whose interview practice pack waits on its role research (D77)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_interview_job_wait"
down_revision: str | None = "0030_outreach_auto"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("interview_intel_request", sa.Column("job_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_interview_intel_request_job_id_job"),
        "interview_intel_request",
        "job",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_interview_intel_request_job_id_job"), "interview_intel_request", type_="foreignkey")
    op.drop_column("interview_intel_request", "job_id")
