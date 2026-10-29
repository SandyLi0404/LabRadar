"""Reduce activity to a single last-year figure, and drop teaching staff.

Two decisions land together here.

The 3- and 5-year windows are replaced by one "last year" count. Three numbers
invited comparison between them when only recency answers the question the app
exists to answer, and the extra columns had to be kept consistent everywhere.

Teaching-track appointments are removed outright rather than filtered in the
API. The department directory's title is authoritative and this project covers
research groups only, so a lecturer should never be in the database at all. That
also makes ``appointment_track`` redundant: everyone stored is research faculty.

The delete cascades in dependency order, and it is the one place in this schema
where rows are removed rather than closed out. That is deliberate: these people
are out of scope, not departed, and keeping them would misrepresent history as
much as deleting a departed professor would.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TEACHING_TITLE_SQL = (
    r"(lecturer|teaching[[:space:]]+professor|instructor"
    r"|adjunct|professor[[:space:]]+of[[:space:]]+practice)"
)


def upgrade() -> None:
    # --- activity columns: three windows down to one -------------------------
    for table in ("faculty_activity", "lab_activity"):
        op.add_column(
            table,
            sa.Column("works_last_year", sa.Integer(), nullable=False, server_default="0"),
        )
    op.add_column(
        "faculty_activity", sa.Column("raw_records_last_year", sa.Integer(), nullable=True)
    )

    # Seed the new column from the old 3-year figure so the table is never
    # empty between this migration and the next compute run. The next run
    # overwrites it with a correctly scoped count.
    op.execute("UPDATE faculty_activity SET works_last_year = works_last_3y")
    op.execute("UPDATE faculty_activity SET raw_records_last_year = raw_records_last_3y")
    op.execute("UPDATE lab_activity SET works_last_year = works_last_3y")

    for table in ("faculty_activity", "lab_activity"):
        op.drop_column(table, "works_last_3y")
        op.drop_column(table, "works_last_5y")
    op.drop_column("faculty_activity", "raw_records_last_3y")

    # --- remove teaching-track people entirely -------------------------------
    # The ids are captured up front. Selecting them from faculty_version inline
    # would silently stop matching once that table's rows are deleted, leaving
    # the faculty rows themselves behind -- which is exactly what happened the
    # first time this was written.
    op.execute(
        f"""
        CREATE TEMP TABLE teaching_faculty ON COMMIT DROP AS
        SELECT DISTINCT faculty_id
          FROM faculty_version
         WHERE title ~* '{TEACHING_TITLE_SQL}'
        """
    )
    teaching = "SELECT faculty_id FROM teaching_faculty"

    # Children first; every one of these references faculty.id.
    for table in (
        "faculty_activity",
        "faculty_work_cluster",
        "faculty_search",
        "faculty_interest_term",
        "faculty_area",
        "faculty_lab",
        "faculty_openalex_link",
        "faculty_version",
    ):
        op.execute(f"DELETE FROM {table} WHERE faculty_id IN ({teaching})")

    op.execute(
        f"""
        DELETE FROM change_event
         WHERE entity_type IN ('faculty', 'activity', 'faculty_area')
           AND entity_id NOT IN (SELECT id FROM faculty)
        """
    )
    op.execute(f"DELETE FROM faculty WHERE id IN ({teaching})")

    # A group left with no members at all is now meaningless.
    op.execute(
        """
        DELETE FROM lab_activity
         WHERE lab_id NOT IN (SELECT DISTINCT lab_id FROM faculty_lab)
        """
    )
    op.execute(
        """
        DELETE FROM lab
         WHERE id NOT IN (SELECT DISTINCT lab_id FROM faculty_lab)
        """
    )

    op.drop_index("ix_faculty_version_track", table_name="faculty_version")
    op.drop_column("faculty_version", "appointment_track")


def downgrade() -> None:
    """Restores the columns. The deleted people are not recoverable here --
    re-run ``labradar fetch-directory`` against a build that keeps them."""
    op.add_column(
        "faculty_version",
        sa.Column("appointment_track", sa.String(16), nullable=False, server_default="research"),
    )
    op.create_index("ix_faculty_version_track", "faculty_version", ["appointment_track"])

    for table in ("faculty_activity", "lab_activity"):
        op.add_column(
            table, sa.Column("works_last_3y", sa.Integer(), nullable=False, server_default="0")
        )
        op.add_column(
            table, sa.Column("works_last_5y", sa.Integer(), nullable=False, server_default="0")
        )
    op.add_column(
        "faculty_activity", sa.Column("raw_records_last_3y", sa.Integer(), nullable=True)
    )
    op.execute("UPDATE faculty_activity SET works_last_3y = works_last_year")
    op.execute("UPDATE lab_activity SET works_last_3y = works_last_year")

    for table in ("faculty_activity", "lab_activity"):
        op.drop_column(table, "works_last_year")
    op.drop_column("faculty_activity", "raw_records_last_year")
