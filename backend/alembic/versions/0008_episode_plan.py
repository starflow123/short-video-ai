"""episodes 增加拆解规划：分段建议与理由

Revision ID: 0008_episode_plan
Revises: 0007_shot_context
Create Date: 2026-10-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_episode_plan"
down_revision = "0007_shot_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 规划不再是一次性的预览接口：拆解时一并产出并落库，
    # 前端据此展示「先按哪几段拆、每段几镜、为什么」，让提示词与拆解分析同源。
    op.add_column("episodes", sa.Column("plan", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("episodes", "plan")
