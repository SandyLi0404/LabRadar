"""Research groups discovered from faculty homepages.

Separate from the seed layer on purpose: labs are a soft signal with a
confidence and a source URL, not department-issued facts.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from labradar.db.models import FacultyLab, Lab, LabActivity

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = (Lab, FacultyLab, LabActivity)


def upgrade() -> None:
    bind = op.get_bind()
    for model in TABLES:
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for model in reversed(TABLES):
        model.__table__.drop(bind=bind, checkfirst=True)
