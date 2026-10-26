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


async def _media_meta(db: AsyncSession, media_ids: set[int]) -> dict[int, dict]:
    if not media_ids:
        return {}
    rows = (
        await db.execute(
            select(MediaAsset.id, MediaAsset.meta).where(MediaAsset.id.in_(media_ids))
        )
    ).all()
    return {row[0]: (row[1] or {}) for row in rows}


def _apply_reference_meta(item: ShotKeyframeRead, meta: dict) -> None:
    """把出图时记录的参考图体检结果挂到关键帧上，前端直接展示「这一帧像不像本人」的证据。"""
    item.reference_count = int(meta.get("reference_count") or 0)
    item.reference_used = meta.get("reference_used")
    item.reference_roles = list(meta.get("reference_roles") or [])
    item.reference_warning = meta.get("reference_warning")
    item.reference_issues = list(meta.get("reference_issues") or [])
    review = meta.get("visual_review")
    item.visual_review = review if isinstance(review, dict) else None


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
    metas = await _media_meta(
        db,
        {
            mid
            for shot in shots
            for mid in (
                [shot.video_media_id]
                + [item.media_id for item in keyframe_map.get(shot.id, [])]
            )
            if mid
        },
    )

    result: list[ShotRead] = []
    for shot in shots:
        item = ShotRead.model_validate(shot)
        item.video_url = urls.get(shot.video_media_id)
        if shot.video_media_id:
            meta = metas.get(shot.video_media_id) or {}
            review = meta.get("continuity_review")
            item.continuity_review = review if isinstance(review, dict) else None
            item.video_note = meta.get("resolution_note")
        item.keyframes = []
        for keyframe in keyframe_map.get(shot.id, []):
            keyframe_item = ShotKeyframeRead(
                id=keyframe.id,
                shot_id=keyframe.shot_id,
                media_id=keyframe.media_id,
                order_index=keyframe.order_index,
                label=keyframe.label,
                prompt=keyframe.prompt,
                is_primary=keyframe.is_primary,
                approved_at=keyframe.approved_at,
                created_at=keyframe.created_at,
                url=urls.get(keyframe.media_id),
            )
            if keyframe.media_id:
                _apply_reference_meta(keyframe_item, metas.get(keyframe.media_id, {}))
            item.keyframes.append(keyframe_item)
        result.append(item)
    return result


async def serialize_keyframe(db: AsyncSession, keyframe: ShotKeyframe) -> ShotKeyframeRead:
    item = ShotKeyframeRead.model_validate(keyframe)
    if keyframe.media_id:
        item.url = (await _media_urls(db, {keyframe.media_id})).get(keyframe.media_id)
        meta = (await _media_meta(db, {keyframe.media_id})).get(keyframe.media_id, {})
        _apply_reference_meta(item, meta)
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
