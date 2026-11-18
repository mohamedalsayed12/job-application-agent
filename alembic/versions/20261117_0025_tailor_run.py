"""Store each paid step of an unfinished tailored CV package, so a crash resumes instead of paying again."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_tailor_run"
down_revision: str | None = "0024_tailor_outside_locations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tailor_run",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tailored_cv_id", sa.Uuid(), sa.ForeignKey("tailored_cv.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("steps", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("crashes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tailored_cv_id", name="uq_tailor_run_tailored_cv_id"),
    )


def downgrade() -> None:
    op.drop_table("tailor_run")
