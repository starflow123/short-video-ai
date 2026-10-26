from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class EvalRun(Base, TimestampMixin):
    """一次金镜头回归：对本批金镜头的现有主关键帧跑视觉判官，与上次对比。

    只产报告，绝不写 approved_at、不触发重绘——与主链路完全隔离。
    """

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )

    kind: Mapped[str] = mapped_column(String(32), default="golden_shot", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    totals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalRunItem(Base, TimestampMixin):
    __tablename__ = "eval_run_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    shot_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    prev_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    changed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    checks: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
