"""add vision capability to existing DeepSeek credentials

Revision ID: 0011_vision_capability
Revises: 0010_keyframe_approved
Create Date: 2026-10-25
"""
from __future__ import annotations

from alembic import op

revision = "0011_vision_capability"
down_revision = "0010_keyframe_approved"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 关键帧视觉审查复用已有的 DeepSeek Key，不需要用户重新添加凭证；
    # 存量行的 capabilities 只有 "text"，这里补上 "vision"。
    op.execute(
        """
        UPDATE provider_credentials
        SET capabilities = CASE
            WHEN capabilities IS NULL OR btrim(capabilities) = '' THEN 'vision'
            ELSE capabilities || ',vision'
        END
        WHERE provider = 'deepseek'
          AND (capabilities IS NULL OR capabilities NOT LIKE '%vision%')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE provider_credentials
        SET capabilities = COALESCE(
            NULLIF(btrim(both ',' from replace(capabilities, 'vision', '')), ''),
            'text'
        )
        WHERE provider = 'deepseek'
          AND capabilities LIKE '%vision%'        """
    )
