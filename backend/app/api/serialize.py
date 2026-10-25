"""API 层的响应组装辅助。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import ProductionAsset
from app.models.keyframe import ShotKeyframe
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.shot import Shot
from app.schemas.asset import AssetRead
from app.schemas.content import ShotKeyframeRead, ShotRead
from app.services.keyframes import load_keyframes

__all__ = ["serialize_shots", "serialize_keyframe", "serialize_assets"]


async def _media_urls(db: AsyncSession, media_ids: set[int]) -> dict[int, str]:
    if not media_ids:
        return {}
    rows = (
        await db.execute(
            select(MediaAsset.id, MediaAsset.url).where(MediaAsset.id.in_(media_ids))
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def serialize_shots(db: AsyncSession, shots: list[Shot]) -> list[ShotRead]:
    """给分镜补全关键帧列表 / 视频的访问地址。"""
    keyframe_map = await load_keyframes(db, [shot.id for shot in shots])
    media_ids = {
        mid
        for shot in shots
        for mid in (
            [shot.video_media_id] + [item.media_id for item in keyframe_map.get(shot.id, [])]
        )
        if mid
    }
    urls = await _media_urls(db, media_ids)

    result: list[ShotRead] = []
    for shot in shots:
        item = ShotRead.model_validate(shot)
        item.video_url = urls.get(shot.video_media_id)
        item.keyframes = [
            ShotKeyframeRead(
                id=keyframe.id,
                shot_id=keyframe.shot_id,
                media_id=keyframe.media_id,
                order_index=keyframe.order_index,
                label=keyframe.label,
                prompt=keyframe.prompt,
                is_primary=keyframe.is_primary,
                created_at=keyframe.created_at,
                url=urls.get(keyframe.media_id),
            )
            for keyframe in keyframe_map.get(shot.id, [])
        ]
        result.append(item)
    return result


async def serialize_keyframe(db: AsyncSession, keyframe: ShotKeyframe) -> ShotKeyframeRead:
    item = ShotKeyframeRead.model_validate(keyframe)
    if keyframe.media_id:
        item.url = (await _media_urls(db, {keyframe.media_id})).get(keyframe.media_id)
    return item


async def serialize_assets(
    db: AsyncSession, assets: list[ProductionAsset]
) -> list[AssetRead]:
    """给资产补全参考图地址与所属项目名（前端卡片直接展示）。"""
    urls = await _media_urls(db, {a.media_id for a in assets if a.media_id})
    project_ids = {a.project_id for a in assets if a.project_id}
    titles: dict[int, str] = {}
    if project_ids:
        rows = (
            await db.execute(select(Project.id, Project.title).where(Project.id.in_(project_ids)))
        ).all()
        titles = {row[0]: row[1] for row in rows}

    result: list[AssetRead] = []
    for asset in assets:
        item = AssetRead.model_validate(asset)
        item.media_url = urls.get(asset.media_id) if asset.media_id else None
        item.project_title = titles.get(asset.project_id) if asset.project_id else None
        result.append(item)
    return result
