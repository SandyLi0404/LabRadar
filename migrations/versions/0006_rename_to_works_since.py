"""Rename the activity counters to say what they actually measure.

``works_last_year`` counted papers from the previous calendar year *and* the
current one, so a card reading "5 papers last year" was really reporting two
years. The span is deliberate -- publication data is year-granularity and
OpenAlex lags by months, so a single year reports working researchers as silent
-- but the name and the label both misdescribed it.

The counts are unchanged. Only the names change, to ``works_since`` /
``raw_records_since``, and the API now returns the cutoff year alongside them so
the interface renders "since 2025" from the same value it counts with. A label
and a number that come from one source cannot drift apart.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("faculty_activity", "works_last_year", new_column_name="works_since")
    op.alter_column(
        "faculty_activity", "raw_records_last_year", new_column_name="raw_records_since"
    )
    op.alter_column("lab_activity", "works_last_year", new_column_name="works_since")


def downgrade() -> None:
    op.alter_column("faculty_activity", "works_since", new_column_name="works_last_year")
    op.alter_column(
        "faculty_activity", "raw_records_since", new_column_name="raw_records_last_year"
    )
    op.alter_column("lab_activity", "works_since", new_column_name="works_last_year")
