"""Application answers, applications, extension device, and apply spend kinds."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_apply_agent"
down_revision: str | None = "0020_tailored_cv_failed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'ai_cv_tailor', "
    "'linkedin_jobs', 'indeed_jobs', 'retired'"
)
_NEW_KINDS = _OLD_KINDS + ", 'apply_form', 'ai_apply_map'"
_PREFER = "prefer_not_to_say"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_NEW_KINDS})")

    op.create_table(
        "application_answers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("legal_first_name", sa.Text(), nullable=True),
        sa.Column("legal_last_name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("address_line", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("github_url", sa.Text(), nullable=True),
        sa.Column("portfolio_url", sa.Text(), nullable=True),
        sa.Column("right_to_work_uk", sa.Boolean(), nullable=True),
        sa.Column("needs_visa_sponsorship", sa.Boolean(), nullable=True),
        sa.Column("nationality", sa.Text(), nullable=True),
        sa.Column("heard_about", sa.Text(), nullable=True),
        sa.Column("previously_employed", sa.Boolean(), nullable=True),
        sa.Column("free_school_meals", sa.Text(), server_default=sa.text(f"'{_PREFER}'"), nullable=False),
        sa.Column("gender", sa.Text(), server_default=sa.text(f"'{_PREFER}'"), nullable=False),
        sa.Column("ethnicity", sa.Text(), server_default=sa.text(f"'{_PREFER}'"), nullable=False),
        sa.Column("disability", sa.Text(), server_default=sa.text(f"'{_PREFER}'"), nullable=False),
        sa.Column("veteran", sa.Text(), server_default=sa.text(f"'{_PREFER}'"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_answers")),
        sa.UniqueConstraint("applicant_id", name=op.f("uq_application_answers_applicant_id")),
    )

    op.create_table(
        "application",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("apply_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("ats_kind", sa.Text(), nullable=True),
        sa.Column("schema_json", postgresql.JSONB(), nullable=True),
        sa.Column("fill_plan_json", postgresql.JSONB(), nullable=True),
        sa.Column("missing_fields", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('preparing', 'ready_to_fill', 'filled_awaiting_review', "
            "'needs_input', 'failed', 'abandoned')",
            name="status",
        ),
        sa.CheckConstraint(
            "ats_kind IS NULL OR ats_kind IN ('greenhouse', 'lever', 'unknown')",
            name="ats_kind",
        ),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application")),
        sa.UniqueConstraint("applicant_id", "job_id", name="uq_application_applicant_job"),
    )
    op.create_index("ix_application_applicant_status", "application", ["applicant_id", "status"])

    op.create_table(
        "extension_device",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), server_default=sa.text("'default'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extension_device")),
        sa.UniqueConstraint("applicant_id", name=op.f("uq_extension_device_applicant_id")),
    )


def downgrade() -> None:
    op.drop_table("extension_device")
    op.drop_index("ix_application_applicant_status", table_name="application")
    op.drop_table("application")
    op.drop_table("application_answers")
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_OLD_KINDS})")
