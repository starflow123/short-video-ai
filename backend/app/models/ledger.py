from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class GenerationLedger(Base, TimestampMixin):
    """生成台账：每个任务终态一行，只记任务侧的不可变事实。

    质量维度（视觉初审结论、人工认可）不冗余在这里——它们发生在任务完成之后，
    查询时经 media_assets / shot_keyframes 关联实时取，永远不失真。
    """

    __tablename__ = "generation_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("generation_tasks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
    shot_id: Mapped[int | None] = mapped_column(
        ForeignKey("shots.id", ondelete="SET NULL"), index=True, nullable=True
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    media_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 编译器指纹：改动 compose_generation_prompt 及其子块函数时变化，
    # 台账因此能区分「同一个 prompt 版本内」的成功率
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
