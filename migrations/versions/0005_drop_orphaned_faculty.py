"""Remove faculty rows left behind by 0004's delete ordering.

Revision 0004 selected the teaching-track ids from ``faculty_version`` inline,
then deleted from that same table before deleting from ``faculty``. By the time
the final statement ran the subquery matched nothing, so eleven people kept a
``faculty`` row with no version, no areas and no activity.

0004 has since been corrected to capture the ids first, so a database built from
scratch never reaches this state. This revision repairs the ones that already
ran the broken version. It keys on the orphaning itself rather than on titles,
because the titles live in the rows 0004 already deleted.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

ORPHANED = """
    SELECT f.id
      FROM faculty f
     WHERE NOT EXISTS (
           SELECT 1 FROM faculty_version v WHERE v.faculty_id = f.id
     )
"""


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TEMP TABLE orphaned_faculty ON COMMIT DROP AS
        {ORPHANED}
        """
    )
    orphans = "SELECT id FROM orphaned_faculty"

    for table in (
        "faculty_activity",
        "faculty_work_cluster",
        "faculty_search",
        "faculty_interest_term",
        "faculty_area",
        "faculty_lab",
        "faculty_openalex_link",
    ):
        op.execute(f"DELETE FROM {table} WHERE faculty_id IN ({orphans})")

    op.execute(f"DELETE FROM faculty WHERE id IN ({orphans})")

    # Change events pointing at people who no longer exist would render as a
    # nameless row in the change feed.
    op.execute(
        """
        DELETE FROM change_event
         WHERE entity_type IN ('faculty', 'activity', 'faculty_area')
           AND entity_id IS NOT NULL
           AND entity_id NOT IN (SELECT id FROM faculty)
        """
    )

    # And any group whose last member just went.
    op.execute(
        """
        DELETE FROM lab_activity
         WHERE lab_id NOT IN (SELECT DISTINCT lab_id FROM faculty_lab)
        """
    )
    op.execute(
        "DELETE FROM lab WHERE id NOT IN (SELECT DISTINCT lab_id FROM faculty_lab)"
    )


def downgrade() -> None:
    """Deleted people cannot be restored here; re-run the directory fetch."""
    pass
