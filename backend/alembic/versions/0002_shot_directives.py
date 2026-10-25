"""add shot.directives

Revision ID: 0002_shot_directives
Revises: 0001_initial
Create Date: 2026-10-17
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_shot_directives"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shots", sa.Column("directives", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("shots", "directives")
