from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# status: active（可注入）/ stale（未被确认或被绕过，暂不注入）/ archived（被取代或废弃）
# approved_at 为空 = 待人工认可，检索永远不会返回它——与关键帧认可同一闸门哲学


class KnowledgeNote(Base, TimestampMixin):
    """生产教训库：agent 与提示词编译器的可注入知识条目。

    - scope：global（跨项目）或 project（只属于一个项目）
    - preconditions：布尔硬过滤条件（如 {"provider": "agnes", "media_type": "video"}），
      检索上下文不满足就不进入候选——防止「治愈短片的教训塞进悬疑剧」
    - superseded_by：被哪条新结论取代，双向留痕可追溯「为什么当时的结论变了」
    """

    __tablename__ = "knowledge_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )

    scope: Mapped[str] = mapped_column(String(16), default="global", nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), default="heuristic", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    preconditions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    visual_style: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
