from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CompilerTemplate(Base, TimestampMixin):
    """全局编译模板的版本化覆盖（RSI 唯一可写的对象）。

    - key 必须在 prompts.TEMPLATE_KEYS 白名单里；
    - is_active=True 的那行是当前生效版本（同一 key 只允许一行 active）；
    - approved_at 为空 = 提案，未认可，永不装载；
    - validated 记录提案时金镜头断言的验证结果——没验证过的不许进队列。
    """

    __tablename__ = "compiler_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    proposed_by_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
