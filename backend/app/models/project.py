from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="9:16", nullable=False)
    style: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    # Jellyfish 对齐：视觉风格 / 视频风格 / 全局种子 / 风格统一开关
    visual_style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    style_lock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    episodes: Mapped[list["Episode"]] = relationship(  # noqa: F821
        back_populates="project", cascade="all, delete-orphan", order_by="Episode.order_index"
    )
