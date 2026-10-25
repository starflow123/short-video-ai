from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# 生产资产（资产管理）：把「可复用的角色 / 场景 / 道具 / 服装」沉淀下来，
# 生成关键帧或视频时按 id 关联，后端会把资产的外观描述拼进提示词，
# 保证跨镜头、跨集数里同一个人 / 同一个场景长得一致。
# - kind: actor（演员/角色） / scene（场景） / prop（道具） / costume（服装）
# - media_id: 参考图（指向 media_assets.id），用于对照或作为图生图的参考
# - source: manual（手动新建） / generated（从已生成的图片存为资产）


class ProductionAsset(Base, TimestampMixin):
    __tablename__ = "production_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True, nullable=True
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 供生成直接使用的、稳定的外观描述（跨镜头一字不改地拼进提示词）
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attributes: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    media_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)
    # 跨项目复用：勾选后其它项目也能在生成时选中该资产
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
