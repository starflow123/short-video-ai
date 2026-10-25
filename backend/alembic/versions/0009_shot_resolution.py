"""shot resolution chosen per shot by the breakdown model

Revision ID: 0009_shot_resolution
Revises: 0008_episode_plan
Create Date: 2026-10-24
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_shot_resolution"
down_revision = "0008_episode_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shots", sa.Column("resolution", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("shots", "resolution")
