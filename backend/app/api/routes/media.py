"""媒体库：列表 / 删除。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.media import MediaAsset
from app.models.user import User
from app.schemas.media import MediaRead
from app.services.references import detach_media

router = APIRouter(prefix="/media", tags=["媒体"])


@router.get("", response_model=list[MediaRead])
async def list_media(
    project_id: int | None = Query(default=None),
    shot_id: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    unlinked: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MediaRead]:
    stmt = select(MediaAsset).where(MediaAsset.user_id == user.id)
    if project_id is not None:
        stmt = stmt.where(MediaAsset.project_id == project_id)
    if unlinked:
        # 智能剪辑等「不属于某个项目」的产物：不归到任何项目卡片下，也要能被找到
        stmt = stmt.where(MediaAsset.project_id.is_(None))
    if shot_id is not None:
        stmt = stmt.where(MediaAsset.shot_id == shot_id)
    if kind:
        stmt = stmt.where(MediaAsset.kind == kind)

    assets = (
        await db.execute(stmt.order_by(MediaAsset.id.desc()).limit(limit))
    ).scalars().all()
    return [MediaRead.model_validate(asset) for asset in assets]


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    asset = await db.get(MediaAsset, media_id)
    if not asset or asset.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在")

    path = Path(asset.path)
    # 先清理所有指向这张素材的引用（关键帧 / 分镜视频 / 资产参考图），再删记录本身，
    # 否则会留下悬空引用：分镜指向不存在的图，后续生成静默拿不到首帧或参考图。
    await detach_media(db, [media_id])
    await db.delete(asset)
    await db.commit()

    try:
        if path.exists():
            path.unlink()
    except OSError:  # noqa: BLE001 - 文件删除失败不影响记录删除
        pass
