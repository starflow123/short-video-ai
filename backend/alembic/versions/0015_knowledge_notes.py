"""knowledge_notes：生产教训库（资格门 + 取代链 + 认可制）

Revision ID: 0015_knowledge_notes
Revises: 0014_eval_runs
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_knowledge_notes"
down_revision = "0014_eval_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_notes",
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
        sa.Column("scope", sa.String(16), nullable=False, server_default="global"),
        sa.Column("category", sa.String(32), nullable=False, server_default="heuristic"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("preconditions", sa.JSON(), nullable=True),
        sa.Column("visual_style", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("source_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_knowledge_notes_user_id", "knowledge_notes", ["user_id"])
    op.create_index("ix_knowledge_notes_project_id", "knowledge_notes", ["project_id"])
    op.create_index("ix_knowledge_notes_scope", "knowledge_notes", ["scope"])
    op.create_index("ix_knowledge_notes_category", "knowledge_notes", ["category"])
    op.create_index("ix_knowledge_notes_status", "knowledge_notes", ["status"])
    op.create_index("ix_knowledge_notes_visual_style", "knowledge_notes", ["visual_style"])


def downgrade() -> None:
    op.drop_table("knowledge_notes")
