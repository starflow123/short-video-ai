from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# 一个分镜可拥有多张关键帧（起幅 / 中段 / 落幅 等），其中恰好一张为「主关键帧」，
# 图生视频默认取主关键帧作为首帧。media_id 指向 media_assets.id，
# 与 media_assets.shot_id 同样是整数引用，由业务层保证一致性。
# approved_at 非空 = 用户已人工认可这一帧；批量生视频只消费已认可的帧。


class ShotKeyframe(Base, TimestampMixin):
    __tablename__ = "shot_keyframes"

    id: Mapped[int] = mapped_column(primary_key=True)
    shot_id: Mapped[int] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    order_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
