"""Several CVs per person, one of them primary, and the CV each tailored package and application used (D65)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_multi_cv"
down_revision: str | None = "0027_search_ideas_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USERS = ("tailored_cv", "application")


def upgrade() -> None:
    op.drop_constraint(op.f("uq_cv_file_applicant_id"), "cv_file", type_="unique")
    op.create_index(op.f("ix_cv_file_applicant_id"), "cv_file", ["applicant_id"])
    op.add_column("cv_file", sa.Column("label", sa.Text(), server_default=sa.text("''"), nullable=False))
    op.add_column("cv_file", sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.execute("UPDATE cv_file SET is_primary = true")
    op.create_index(
        "uq_cv_file_one_primary", "cv_file", ["applicant_id"], unique=True, postgresql_where=sa.text("is_primary")
    )
    for table in _USERS:
        op.add_column(table, sa.Column("cv_file_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_cv_file_id_cv_file"), table, "cv_file", ["cv_file_id"], ["id"], ondelete="SET NULL"
        )
    # Until now each person had one CV, so it is the one every existing package used.
    op.execute("UPDATE tailored_cv t SET cv_file_id = c.id FROM cv_file c WHERE c.applicant_id = t.applicant_id")
    op.execute(
        "UPDATE application a SET cv_file_id = c.id FROM cv_file c "
        "WHERE c.applicant_id = a.applicant_id AND a.fill_plan_json IS NOT NULL"
    )


def downgrade() -> None:
    extra = op.get_bind().scalar(sa.text("SELECT count(*) FROM cv_file WHERE NOT is_primary"))
    if extra:
        raise RuntimeError(f"{extra} CVs are not their owner's primary CV. Delete them before downgrading.")
    for table in _USERS:
        op.drop_constraint(op.f(f"fk_{table}_cv_file_id_cv_file"), table, type_="foreignkey")
        op.drop_column(table, "cv_file_id")
    op.drop_index("uq_cv_file_one_primary", table_name="cv_file")
    op.drop_column("cv_file", "is_primary")
    op.drop_column("cv_file", "label")
    op.drop_index(op.f("ix_cv_file_applicant_id"), table_name="cv_file")
    op.create_unique_constraint(op.f("uq_cv_file_applicant_id"), "cv_file", ["applicant_id"])
