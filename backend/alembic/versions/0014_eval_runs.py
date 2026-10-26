"""eval_runs：金镜头生成回归（判官只产报告，与主链路隔离）

Revision ID: 0014_eval_runs
Revises: 0013_generation_ledger
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_eval_runs"
down_revision = "0013_generation_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_runs",
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
        sa.Column("kind", sa.String(32), nullable=False, server_default="golden_shot"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column("totals", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_eval_runs_user_id", "eval_runs", ["user_id"])
    op.create_index("ix_eval_runs_project_id", "eval_runs", ["project_id"])

    op.create_table(
        "eval_run_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("shot_id", sa.Integer(), nullable=True),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("prev_verdict", sa.String(16), nullable=True),
        sa.Column("changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checks", sa.JSON(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_eval_run_items_run_id", "eval_run_items", ["run_id"])
    op.create_index("ix_eval_run_items_shot_id", "eval_run_items", ["shot_id"])


def downgrade() -> None:
    op.drop_table("eval_run_items")
    op.drop_table("eval_runs")
