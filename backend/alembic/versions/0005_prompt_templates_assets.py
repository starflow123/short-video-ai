"""prompt_templates + production_assets：提示词模板库与资产管理

Revision ID: 0005_prompt_templates_assets
Revises: 0004_project_display_fields
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.services.prompt_templates import DEFAULT_TEMPLATES, extract_variables

revision = "0005_prompt_templates_assets"
down_revision = "0004_project_display_fields"
branch_labels = None
depends_on = None


prompt_templates = sa.table(
    "prompt_templates",
    sa.column("user_id", sa.Integer()),
    sa.column("category", sa.String()),
    sa.column("name", sa.String()),
    sa.column("preview", sa.String()),
    sa.column("content", sa.Text()),
    sa.column("variables", sa.JSON()),
    sa.column("is_system", sa.Boolean()),
    sa.column("is_default", sa.Boolean()),
    sa.column("sort_index", sa.Integer()),
)


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("preview", sa.String(255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_prompt_templates_user_id", "prompt_templates", ["user_id"])
    op.create_index("ix_prompt_templates_category", "prompt_templates", ["category"])

    op.create_table(
        "production_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=True),
        sa.Column("media_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("is_global", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_production_assets_user_id", "production_assets", ["user_id"])
    op.create_index("ix_production_assets_project_id", "production_assets", ["project_id"])
    op.create_index("ix_production_assets_kind", "production_assets", ["kind"])

    # 内置提示词模板：随迁移入库，后续用户可在「提示词模板」页另存 / 调整自己的版本
    op.bulk_insert(
        prompt_templates,
        [
            {
                "user_id": None,
                "category": item["category"],
                "name": item["name"],
                "preview": item.get("preview"),
                "content": item["content"],
                "variables": item.get("variables") or extract_variables(item["content"]),
                "is_system": True,
                "is_default": bool(item.get("is_default")),
                "sort_index": index * 10,
            }
            for index, item in enumerate(DEFAULT_TEMPLATES)
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_production_assets_kind", table_name="production_assets")
    op.drop_index("ix_production_assets_project_id", table_name="production_assets")
    op.drop_index("ix_production_assets_user_id", table_name="production_assets")
    op.drop_table("production_assets")

    op.drop_index("ix_prompt_templates_category", table_name="prompt_templates")
    op.drop_index("ix_prompt_templates_user_id", table_name="prompt_templates")
    op.drop_table("prompt_templates")
