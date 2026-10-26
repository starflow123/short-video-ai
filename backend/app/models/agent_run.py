from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AgentRun(Base, TimestampMixin):
    """一次 agent 运行的审计记录：谁、对什么、用了几步、结论与完整轨迹。"""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trace: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    advisory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
