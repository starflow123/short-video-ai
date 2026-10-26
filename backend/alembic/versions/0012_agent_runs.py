"""agent_runs：统一记录修复 / 导演自检 / 失败诊断 / 切点修复 / 素材整理

Revision ID: 0012_agent_runs
Revises: 0011_vision_capability
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_agent_runs"
down_revision = "0011_vision_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.String(64), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("trace", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("advisory", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_project_id", "agent_runs", ["project_id"])
    op.create_index("ix_agent_runs_kind", "agent_runs", ["kind"])
    op.create_index("ix_agent_runs_target", "agent_runs", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_target", table_name="agent_runs")
    op.drop_index("ix_agent_runs_kind", table_name="agent_runs")
    op.drop_index("ix_agent_runs_project_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_table("agent_runs")
