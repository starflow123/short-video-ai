"""pipeline_runs + shots.prompt_confirmed：制作流水线（LangGraph 编排）状态

Revision ID: 0006_pipeline_runs
Revises: 0005_prompt_templates_assets
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_pipeline_runs"
down_revision = "0005_prompt_templates_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "episode_id", sa.Integer(), sa.ForeignKey("episodes.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(32), nullable=False, server_default="start"),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("characters", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_pipeline_runs_user_id", "pipeline_runs", ["user_id"])
    op.create_index("ix_pipeline_runs_project_id", "pipeline_runs", ["project_id"])
    op.create_index("ix_pipeline_runs_episode_id", "pipeline_runs", ["episode_id"])

    # 分镜的人工确认位：AI 出分镜后先停下来等用户逐个审阅提示词
    op.add_column(
        "shots",
        sa.Column(
            "prompt_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("shots", "prompt_confirmed")
    op.drop_index("ix_pipeline_runs_episode_id", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_project_id", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_user_id", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
