"""shot_keyframes: 分镜支持多张关键帧

Revision ID: 0003_shot_keyframes
Revises: 0002_shot_directives
Create Date: 2026-10-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_shot_keyframes"
down_revision = "0002_shot_directives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shot_keyframes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "shot_id",
            sa.Integer(),
            sa.ForeignKey("shots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("label", sa.String(100), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shot_keyframes_shot_id", "shot_keyframes", ["shot_id"])

    # 存量数据迁移：shots.keyframe_media_id → 该分镜的第 1 张主关键帧
    op.execute(
        """
        INSERT INTO shot_keyframes
            (shot_id, media_id, order_index, label, is_primary, created_at, updated_at)
        SELECT id, keyframe_media_id, 1, '关键帧 1', true, now(), now()
        FROM shots
        WHERE keyframe_media_id IS NOT NULL
        """
    )
    op.drop_column("shots", "keyframe_media_id")


def downgrade() -> None:
    op.add_column("shots", sa.Column("keyframe_media_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE shots SET keyframe_media_id = (
            SELECT k.media_id FROM shot_keyframes k
            WHERE k.shot_id = shots.id AND k.is_primary = true
            ORDER BY k.order_index LIMIT 1
        )
        """
    )
    op.drop_index("ix_shot_keyframes_shot_id", table_name="shot_keyframes")
    op.drop_table("shot_keyframes")
