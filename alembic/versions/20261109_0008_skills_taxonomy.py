"""Skill taxonomy, the names each skill goes by, and skills found in jobs (D39)"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_skills_taxonomy"
down_revision: str | None = "0007_profile_and_cv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "skill",
        _id(),
        sa.Column("taxonomy_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("preferred_label", sa.Text(), nullable=False),
        sa.Column("alternative_labels", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("is_technical", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source IN ('esco', 'local')", name=op.f("ck_skill_source")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill")),
        sa.UniqueConstraint("taxonomy_id", name="uq_skill_taxonomy_id"),
    )
    op.create_table(
        "skill_label",
        _id(),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("normalized", sa.Text(), nullable=False),
        sa.Column("is_preferred", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skill.id"], name=op.f("fk_skill_label_skill_id_skill"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_label")),
        sa.UniqueConstraint("skill_id", "normalized", name="uq_skill_label_skill_id_normalized"),
    )
    op.create_index(
        "ix_skill_label_normalized", "skill_label", ["normalized"], postgresql_ops={"normalized": "text_pattern_ops"}
    )
    op.create_table(
        "job_skill",
        _id(),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("matched_text", sa.Text(), nullable=False),
        sa.Column("how_matched", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("how_matched IN ('exact', 'alternative_label')", name=op.f("ck_job_skill_how_matched")),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], name=op.f("fk_job_skill_job_id_job"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skill.id"], name=op.f("fk_job_skill_skill_id_skill"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_skill")),
        sa.UniqueConstraint("job_id", "skill_id", name="uq_job_skill_job_id_skill_id"),
    )
    op.create_index("ix_job_skill_skill_id", "job_skill", ["skill_id"])
    op.add_column("applicant_skill", sa.Column("skill_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_applicant_skill_skill_id_skill", "applicant_skill", "skill", ["skill_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_applicant_skill_skill_id_skill", "applicant_skill", type_="foreignkey")
    op.drop_column("applicant_skill", "skill_id")
    op.drop_table("job_skill")
    op.drop_index("ix_skill_label_normalized", table_name="skill_label")
    op.drop_table("skill_label")
    op.drop_table("skill")
