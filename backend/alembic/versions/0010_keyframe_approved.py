"""keyframe approval gate for video generation

Revision ID: 0010_keyframe_approved
Revises: 0009_shot_resolution
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_keyframe_approved"
down_revision = "0009_shot_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shot_keyframes",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 只有「已经用它出过视频」的镜头才算用户事实上认可过；没有视频的存量帧保持未认可，
    # 交给用户逐张看过再点「认可」。每个镜头只认主帧（没有主帧则取已出图的第一张）。
    op.execute(
        """
        UPDATE shot_keyframes AS k
        SET approved_at = now()
        WHERE k.media_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM shots AS s
              WHERE s.id = k.shot_id AND s.video_media_id IS NOT NULL
          )
          AND k.id = (
              SELECT k2.id
              FROM shot_keyframes AS k2
              WHERE k2.shot_id = k.shot_id AND k2.media_id IS NOT NULL
              ORDER BY k2.is_primary DESC, k2.order_index ASC, k2.id ASC
              LIMIT 1
          )
        """
    )


def downgrade() -> None:
    op.drop_column("shot_keyframes", "approved_at")
