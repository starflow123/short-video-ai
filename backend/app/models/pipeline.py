from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# 制作流水线（LangGraph 编排）：把「建角色档案 → AI 出分镜 → 人工确认提示词 → 批量生图」
# 这条高层流程的状态落库。Celery 负责实际的文本 / 图片 / 视频生成，本表只记录编排进度，
# 让「人工确认」这个中断点可以跨请求、跨进程恢复。
# - status: pending / running / reviewing（等待人工确认） / generating / completed / failed
# - characters: AI 抽取并落库的角色档案摘要（角色名 / 外观 / 服装 / 资产 id）


class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    episode_id: Mapped[int | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), index=True, nullable=True
    )

    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="start", nullable=False)

    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    characters: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
