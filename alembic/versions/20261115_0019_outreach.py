"""Outreach contacts, drafts, sends and Gmail connection."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_outreach"
down_revision: str | None = "0021_application_submitted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = (
    "'ai_summary', 'ai_cv_read', 'ai_job_judge', 'ai_fit_report', 'ai_cv_tailor', "
    "'linkedin_jobs', 'indeed_jobs', 'apply_form', 'ai_apply_map', 'retired'"
)
_KINDS = _OLD_KINDS.replace("'retired'", "'linkedin_people', 'linkedin_profile', 'ai_outreach', 'retired'")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_KINDS})")

    op.create_table(
        "outreach_contact",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("company_name", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("role_kind", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("source_actor", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "role_kind IN ('hiring_manager', 'recruiter', 'executive', 'employee', 'unknown')",
            name=op.f("ck_outreach_contact_role_kind"),
        ),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("applicant_id", "job_id", "linkedin_url", name="uq_outreach_contact_applicant_job_linkedin"),
    )
    op.create_index("ix_outreach_contact_applicant_job", "outreach_contact", ["applicant_id", "job_id"])

    op.create_table(
        "outreach_research",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("about_text", sa.Text(), nullable=True),
        sa.Column("quotes", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("raw_hash", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contact_id"], ["outreach_contact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_id", name="uq_outreach_research_contact"),
    )

    op.create_table(
        "outreach_draft",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("pattern_ids", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("evidence_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("user_edited_body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("channel IN ('gmail', 'linkedin_note')", name=op.f("ck_outreach_draft_channel")),
        sa.CheckConstraint(
            "status IN ('draft', 'edited', 'sent', 'copied')", name=op.f("ck_outreach_draft_status")
        ),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["outreach_contact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "applicant_id",
            "contact_id",
            "channel",
            "evidence_digest",
            name="uq_outreach_draft_evidence",
        ),
    )
    op.create_index("ix_outreach_draft_applicant_contact", "outreach_draft", ["applicant_id", "contact_id"])

    op.create_table(
        "outreach_send",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("channel IN ('gmail', 'linkedin_note')", name=op.f("ck_outreach_send_channel")),
        sa.CheckConstraint(
            "status IN ('sent', 'copied', 'failed', 'dry_run')", name=op.f("ck_outreach_send_status")
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["outreach_draft.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outreach_send_draft", "outreach_send", ["draft_id"])

    op.create_table(
        "gmail_connection",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("applicant_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("refresh_token_sealed", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("applicant_id", name="uq_gmail_connection_applicant"),
    )


def downgrade() -> None:
    op.drop_table("gmail_connection")
    op.drop_index("ix_outreach_send_draft", table_name="outreach_send")
    op.drop_table("outreach_send")
    op.drop_index("ix_outreach_draft_applicant_contact", table_name="outreach_draft")
    op.drop_table("outreach_draft")
    op.drop_table("outreach_research")
    op.drop_index("ix_outreach_contact_applicant_job", table_name="outreach_contact")
    op.drop_table("outreach_contact")
    op.drop_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", type_="check")
    op.create_check_constraint(op.f("ck_spend_ledger_kind"), "spend_ledger", f"kind IN ({_OLD_KINDS})")
