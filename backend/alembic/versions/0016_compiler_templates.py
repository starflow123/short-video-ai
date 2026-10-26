"""compiler_templates：RSI 受限版的全局模板版本表

Revision ID: 0016_compiler_templates
Revises: 0015_knowledge_notes
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_compiler_templates"
down_revision = "0015_knowledge_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compiler_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("validated", sa.JSON(), nullable=True),
        sa.Column("proposed_by_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_compiler_templates_user_id", "compiler_templates", ["user_id"])
    op.create_index("ix_compiler_templates_key", "compiler_templates", ["key"])
    op.create_index("ix_compiler_templates_is_active", "compiler_templates", ["is_active"])


def downgrade() -> None:
    op.drop_table("compiler_templates")
