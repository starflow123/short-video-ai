"""关键帧领域逻辑：查询、主帧选取、生成结果落位。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.keyframe import ShotKeyframe
from app.models.shot import Shot


async def load_keyframes(
    db: AsyncSession, shot_ids: list[int]
) -> dict[int, list[ShotKeyframe]]:
    """一次性取出这些分镜的全部关键帧，按 shot_id 分组并按展示顺序排列。"""
    if not shot_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(ShotKeyframe)
                .where(ShotKeyframe.shot_id.in_(shot_ids))
                .order_by(ShotKeyframe.order_index, ShotKeyframe.id)
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[int, list[ShotKeyframe]] = {}
    for keyframe in rows:
        grouped.setdefault(keyframe.shot_id, []).append(keyframe)
    return grouped


def primary_keyframe(keyframes: list[ShotKeyframe]) -> ShotKeyframe | None:
    """主关键帧：优先「已标记为主帧且已出图」的那张，其次任意已出图的第一张。"""
    if not keyframes:
        return None
    flagged = next((item for item in keyframes if item.is_primary), None)
    if flagged and flagged.media_id:
        return flagged
    with_media = next((item for item in keyframes if item.media_id), None)
    return with_media or flagged or keyframes[0]


async def set_primary(db: AsyncSession, shot_id: int, keyframe_id: int) -> None:
    """把某张关键帧设为主帧（图生视频的默认首帧），其余取消主帧标记。"""
    await db.execute(
        update(ShotKeyframe)
        .where(ShotKeyframe.shot_id == shot_id, ShotKeyframe.id != keyframe_id)
        .values(is_primary=False)
    )
    await db.execute(
        update(ShotKeyframe).where(ShotKeyframe.id == keyframe_id).values(is_primary=True)
    )


async def approve(db: AsyncSession, keyframe: ShotKeyframe) -> None:
    """人工认可某一帧：同一镜头只保留这一帧为已认可，并同步设为主帧。

    批量生视频只消费已认可的帧，所以「认可」既是质量闸门，也是首帧选择。
    """
    await db.execute(
        update(ShotKeyframe)
        .where(ShotKeyframe.shot_id == keyframe.shot_id, ShotKeyframe.id != keyframe.id)
        .values(approved_at=None)
    )
    keyframe.approved_at = datetime.now(timezone.utc)
    await set_primary(db, keyframe.shot_id, keyframe.id)


async def attach_generated_image(
    db: AsyncSession,
    *,
    shot: Shot,
    media_id: int,
    prompt: str,
    keyframe_id: int | None = None,
    label: str | None = None,
    promote_primary: bool = False,
) -> ShotKeyframe:
    """把生成好的图片写入关键帧：指定 keyframe_id 则重绘该张，否则追加一张新关键帧。

    该分镜的第一张关键帧自动成为主帧，保证图生视频有默认首帧。
    promote_primary 用于「在已有画面的基础上新出这一张」的场景（改完提示词重出、
    流水线重跑这批镜头）：新图必须接管主帧，否则图生视频仍在动画被取代的旧图。
    """
    existing = (await load_keyframes(db, [shot.id])).get(shot.id, [])
    keyframe = await db.get(ShotKeyframe, int(keyframe_id)) if keyframe_id else None
    if keyframe is None:
        order_index = max((item.order_index for item in existing), default=0) + 1
        keyframe = ShotKeyframe(
            shot_id=shot.id,
            order_index=order_index,
            label=(label or f"关键帧 {order_index}")[:100],
            prompt=prompt,
            is_primary=not existing,
        )
        db.add(keyframe)
        await db.flush()
        if promote_primary and existing:
            await set_primary(db, shot.id, keyframe.id)
    # 画面一变，原来的「已认可」就失效：用户认可的是一张具体的图，不是这个位置。
    # 清空 approved_at 后该帧会退出批量生视频，必须重新看过再认可。
    if keyframe.media_id and keyframe.media_id != media_id:
        keyframe.approved_at = None
    keyframe.media_id = media_id
    # 始终用本次实际使用的提示词覆盖，保证「重绘」后关键帧上的提示词与实际出图一致
    if prompt:
        keyframe.prompt = prompt
    if keyframe.is_primary and keyframe.id is not None:
        await set_primary(db, shot.id, keyframe.id)
    shot.status = "prepared"
    await db.flush()
    return keyframe
