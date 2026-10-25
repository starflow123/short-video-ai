"""projects: 补 visual_style / video_style / seed / style_lock

Revision ID: 0004_project_display_fields
Revises: 0003_shot_keyframes
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_project_display_fields"
down_revision = "0003_shot_keyframes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("visual_style", sa.String(64), nullable=True))
    op.add_column("projects", sa.Column("video_style", sa.String(64), nullable=True))
    op.add_column("projects", sa.Column("seed", sa.Integer(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("style_lock", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("projects", "style_lock")
    op.drop_column("projects", "seed")
    op.drop_column("projects", "video_style")
    op.drop_column("projects", "visual_style")
