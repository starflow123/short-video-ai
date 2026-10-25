"""shots 结构化镜头上下文：主体 / 道具 / 动作时间轴 / 首尾状态

Revision ID: 0007_shot_context
Revises: 0006_pipeline_runs
Create Date: 2026-10-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_shot_context"
down_revision = "0006_pipeline_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 这些字段是「同一镜头里谁在做什么、道具在谁手里、第几秒发生什么」的结构化事实，
    # 生成时直接注入提示词与参考图选择，不再让模型从一段散文里猜。
    op.add_column("shots", sa.Column("subjects", sa.JSON(), nullable=True))
    op.add_column("shots", sa.Column("props", sa.JSON(), nullable=True))
    op.add_column("shots", sa.Column("action_timeline", sa.JSON(), nullable=True))
    op.add_column("shots", sa.Column("start_state", sa.JSON(), nullable=True))
    op.add_column("shots", sa.Column("end_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("shots", "end_state")
    op.drop_column("shots", "start_state")
    op.drop_column("shots", "action_timeline")
    op.drop_column("shots", "props")
    op.drop_column("shots", "subjects")
