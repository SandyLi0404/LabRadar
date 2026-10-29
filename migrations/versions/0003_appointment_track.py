"""Label each appointment as research or teaching track.

Stored rather than derived at query time so it is historied like every other
directory fact, and so the filter can be expressed in SQL.

Existing rows are backfilled from the title they already carry.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "faculty_version",
        sa.Column(
            "appointment_track",
            sa.String(16),
            nullable=False,
            server_default="research",
        ),
    )
    # Backfill from the titles already stored, using the same pattern the
    # parser applies, so old rows agree with new ones.
    op.execute(
        """
        UPDATE faculty_version
           SET appointment_track = 'teaching'
         WHERE title ~* '(lecturer|teaching[[:space:]]+professor|instructor'
                        '|adjunct|professor[[:space:]]+of[[:space:]]+practice)'
        """
    )
    op.create_index(
        "ix_faculty_version_track", "faculty_version", ["appointment_track"]
    )


def downgrade() -> None:
    op.drop_index("ix_faculty_version_track", table_name="faculty_version")
    op.drop_column("faculty_version", "appointment_track")
