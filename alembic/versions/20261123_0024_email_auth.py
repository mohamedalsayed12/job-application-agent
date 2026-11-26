"""Email verification and password reset: a confirmed-at time on each account, and one-time emailed links.

Accounts that already have a password count as confirmed, so nobody already using the app is locked out.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_email_auth"
down_revision: str | None = "0023_onboarding_wizard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("applicant", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE applicant SET email_verified_at = created_at WHERE password_hash IS NOT NULL")
    op.create_table(
        "auth_token",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("purpose IN ('verify_email', 'reset_password')", name=op.f("ck_auth_token_purpose")),
        sa.ForeignKeyConstraint(
            ["applicant_id"], ["applicant.id"], name=op.f("fk_auth_token_applicant_id_applicant"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_token")),
        sa.UniqueConstraint("token_hash", name="uq_auth_token_token_hash"),
    )
    op.create_index("ix_auth_token_applicant_id_purpose", "auth_token", ["applicant_id", "purpose"])


def downgrade() -> None:
    op.drop_index("ix_auth_token_applicant_id_purpose", table_name="auth_token")
    op.drop_table("auth_token")
    op.drop_column("applicant", "email_verified_at")
