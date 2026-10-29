"""Paper links and dates, for showing each professor's recent papers.

``openalex_work`` gains the publication date (to order papers within a year)
and a link a reader can open. ``faculty_work_cluster`` gains the link chosen
across its records, so an arXiv version's link survives even when the journal
version represents the paper. Existing rows fill in on the next OpenAlex run.

0001 builds tables from the current models, so on a fresh database these
columns already exist. Each one is added only if it is missing; adding it
unconditionally is what broke the original migration chain.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

COLUMNS = (
    ("openalex_work", sa.Column("publication_date", sa.Date(), nullable=True)),
    ("openalex_work", sa.Column("url", sa.Text(), nullable=True)),
    ("faculty_work_cluster", sa.Column("url", sa.Text(), nullable=True)),
)


def _existing(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, column in COLUMNS:
        if column.name not in _existing(table):
            op.add_column(table, column)


def downgrade() -> None:
    for table, column in reversed(COLUMNS):
        if column.name in _existing(table):
            op.drop_column(table, column.name)
