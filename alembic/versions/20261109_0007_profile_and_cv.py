"""Profile, skills, certifications and CV tables, and account deletion rules (D38)"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CITEXT

from alembic import op

revision: str = "0007_profile_and_cv"
down_revision: str | None = "0006_applicant_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPLICANT_FKS = (
    ("applicant_preference", "fk_applicant_preference_applicant_id_applicant", "CASCADE"),
    ("applicant_job_match", "fk_applicant_job_match_applicant_id_applicant", "CASCADE"),
    ("search_run", "fk_search_run_applicant_id_applicant", "SET NULL"),
)


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _applicant_id() -> sa.Column:
    return sa.Column("applicant_id", sa.Uuid(), nullable=False)


def _applicant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["applicant_id"], ["applicant.id"], name=op.f(f"fk_{table}_applicant_id_applicant"), ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "applicant_profile",
        _id(),
        _applicant_id(),
        sa.Column("experience_level", sa.Text(), nullable=False),
        sa.Column("travel_distance_miles", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "experience_level IN ('internship', 'entry', 'mid', 'senior')",
            name=op.f("ck_applicant_profile_experience_level"),
        ),
        sa.CheckConstraint(
            "travel_distance_miles IS NULL OR travel_distance_miles BETWEEN 1 AND 500",
            name=op.f("ck_applicant_profile_travel_distance_miles"),
        ),
        _applicant_fk("applicant_profile"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applicant_profile")),
        sa.UniqueConstraint("applicant_id", name=op.f("uq_applicant_profile_applicant_id")),
    )
    op.create_table(
        "applicant_skill",
        _id(),
        _applicant_id(),
        sa.Column("text_as_typed", CITEXT(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _applicant_fk("applicant_skill"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applicant_skill")),
        sa.UniqueConstraint("applicant_id", "text_as_typed", name="uq_applicant_skill_applicant_id_text_as_typed"),
    )
    op.create_table(
        "applicant_certification",
        _id(),
        _applicant_id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=True),
        sa.Column("is_licence", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _applicant_fk("applicant_certification"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applicant_certification")),
    )
    op.create_index(
        op.f("ix_applicant_certification_applicant_id"), "applicant_certification", ["applicant_id"]
    )
    op.create_table(
        "cv_file",
        _id(),
        _applicant_id(),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("file_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("file_type IN ('pdf', 'docx')", name=op.f("ck_cv_file_file_type")),
        sa.CheckConstraint("size_bytes BETWEEN 1 AND 5242880", name=op.f("ck_cv_file_size_bytes")),
        _applicant_fk("cv_file"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cv_file")),
        sa.UniqueConstraint("applicant_id", name=op.f("uq_cv_file_applicant_id")),
    )

    op.alter_column("search_run", "applicant_id", existing_type=sa.Uuid(), nullable=True)
    for table, name, on_delete in _APPLICANT_FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "applicant", ["applicant_id"], ["id"], ondelete=on_delete)


def downgrade() -> None:
    for table, name, _ in _APPLICANT_FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "applicant", ["applicant_id"], ["id"])
    # Fails if deleted accounts have left search runs behind, rather than dropping that record.
    op.alter_column("search_run", "applicant_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_table("cv_file")
    op.drop_index(op.f("ix_applicant_certification_applicant_id"), table_name="applicant_certification")
    op.drop_table("applicant_certification")
    op.drop_table("applicant_skill")
    op.drop_table("applicant_profile")
