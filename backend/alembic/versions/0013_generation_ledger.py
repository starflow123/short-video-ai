"""generation_ledger：每个生成任务终态一行的旁路台账

Revision ID: 0013_generation_ledger
Revises: 0012_agent_runs
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_generation_ledger"
down_revision = "0012_agent_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("generation_tasks.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "shot_id",
            sa.Integer(),
            sa.ForeignKey("shots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_generation_ledger_user_id", "generation_ledger", ["user_id"])
    op.create_index("ix_generation_ledger_project_id", "generation_ledger", ["project_id"])
    op.create_index("ix_generation_ledger_shot_id", "generation_ledger", ["shot_id"])
    op.create_index("ix_generation_ledger_kind", "generation_ledger", ["kind"])
    op.create_index("ix_generation_ledger_provider", "generation_ledger", ["provider"])
    op.create_index("ix_generation_ledger_status", "generation_ledger", ["status"])
    op.create_index("ix_generation_ledger_media_id", "generation_ledger", ["media_id"])


def downgrade() -> None:
    op.drop_table("generation_ledger")
