"""分镜：详情 / 更新 / 删除 / 关键帧与视频生成。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.serialize import serialize_keyframe, serialize_shots
from app.db.session import get_db
from app.models.episode import Episode
from app.models.keyframe import ShotKeyframe
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.shot import Shot
from app.models.user import User
from app.schemas.content import (
    PromptPreviewRead,
    PromptPreviewRequest,
    ShotFieldSuggestRead,
    ShotFieldSuggestRequest,
    ShotKeyframeCreate,
    ShotKeyframeRead,
    ShotKeyframeUpdate,
    ShotNeighborRead,
    ShotRead,
    ShotUpdate,
)
from app.schemas.task import GenerationRequest, TaskRead
from app.services.dispatch import create_task
from app.services.generation import build_prompt_preview, load_shot_neighbors, suggest_shot_field
from app.services.keyframes import load_keyframes, primary_keyframe, set_primary
from app.services.references import media_of_shots, purge_media, remove_media_files

router = APIRouter(prefix="/shots", tags=["分镜"])


async def _get_owned_shot(db: AsyncSession, user_id: int, shot_id: int) -> Shot:
    shot = await db.get(Shot, shot_id)
    if not shot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分镜不存在")
    episode = await db.get(Episode, shot.episode_id)
    project = await db.get(Project, episode.project_id) if episode else None
    if not project or project.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分镜不存在")
    return shot


async def _get_owned_keyframe(
    db: AsyncSession, shot: Shot, keyframe_id: int
) -> ShotKeyframe:
    keyframe = await db.get(ShotKeyframe, keyframe_id)
    if not keyframe or keyframe.shot_id != shot.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="关键帧不存在")
    return keyframe


def _neighbor_read(shot: Shot | None) -> ShotNeighborRead | None:
    """把相邻镜头缩成前端展示用的简要信息（标题 + 最代表其画面的文本）。"""
    if not shot:
        return None
    summary = (shot.image_prompt or shot.description or shot.title or "").strip() or None
    return ShotNeighborRead(
        id=shot.id, order_index=shot.order_index, title=shot.title, summary=summary
    )


@router.get("/{shot_id}", response_model=ShotRead)
async def get_shot(
    shot_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShotRead:
    shot = await _get_owned_shot(db, user.id, shot_id)
    item = (await serialize_shots(db, [shot]))[0]
    # 附带前后镜头上下文：生成关键帧 / 视频时后端会自动带入，这里让前端可见
    prev_shot, next_shot = await load_shot_neighbors(db, shot)
    item.prev_shot = _neighbor_read(prev_shot)
    item.next_shot = _neighbor_read(next_shot)
    return item


@router.patch("/{shot_id}", response_model=ShotRead)
async def update_shot(
    shot_id: int,
    payload: ShotUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShotRead:
    shot = await _get_owned_shot(db, user.id, shot_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(shot, field, value)
    await db.commit()
    await db.refresh(shot)
    return (await serialize_shots(db, [shot]))[0]


@router.post("/{shot_id}/suggest", response_model=ShotFieldSuggestRead)
async def suggest_shot_field_route(
    shot_id: int,
    payload: ShotFieldSuggestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShotFieldSuggestRead:
    """AI 生成分镜的单个字段（画面描述 / 关键帧提示词 / 视频提示词），同步返回。"""
    shot = await _get_owned_shot(db, user.id, shot_id)
    value = await suggest_shot_field(
        db,
        user_id=user.id,
        shot=shot,
        field=payload.field,
        hint=payload.hint,
        credential_id=payload.credential_id,
        provider_key=payload.provider,
        model=payload.model,
    )
    return ShotFieldSuggestRead(field=payload.field, value=value)


@router.post("/{shot_id}/prompt-preview", response_model=PromptPreviewRead)
async def preview_shot_prompt(
    shot_id: int,
    payload: PromptPreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromptPreviewRead:
    """拼装预览：按当前分层字段拼出真实提示词，并给出各层字数与是否被裁。"""
    shot = await _get_owned_shot(db, user.id, shot_id)
    kind = payload.kind if payload.kind in ("image", "video") else "image"
    data = await build_prompt_preview(
        db, user_id=user.id, shot=shot, kind=kind, extra_prompt=payload.prompt
    )
    return PromptPreviewRead(**data)


@router.delete("/{shot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shot(
    shot_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除分镜：关键帧图片与分镜视频若只属于这一镜，一并彻底删除（记录 + 磁盘文件）。"""
    shot = await _get_owned_shot(db, user.id, shot_id)
    owned = await media_of_shots(db, [shot.id])
    await db.delete(shot)
    await db.flush()
    paths = await purge_media(db, owned)
    await db.commit()
    remove_media_files(paths)


# ---------------------------------------------------------------- 关键帧
@router.get("/{shot_id}/keyframes", response_model=list[ShotKeyframeRead])
async def list_keyframes(
    shot_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ShotKeyframeRead]:
    shot = await _get_owned_shot(db, user.id, shot_id)
    keyframes = (await load_keyframes(db, [shot.id])).get(shot.id, [])
    return [await serialize_keyframe(db, item) for item in keyframes]


@router.post(
    "/{shot_id}/keyframes",
    response_model=ShotKeyframeRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_keyframe(
    shot_id: int,
    payload: ShotKeyframeCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShotKeyframeRead:
    """在分镜下新增一张关键帧（media_id 留空即先占位，稍后生成图片）。"""
    shot = await _get_owned_shot(db, user.id, shot_id)
    if payload.media_id is not None:
        asset = await db.get(MediaAsset, payload.media_id)
        if not asset or asset.user_id != user.id or asset.kind != "image":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="关键帧素材无效")

    existing = (await load_keyframes(db, [shot.id])).get(shot.id, [])
    order_index = payload.order_index or (max((k.order_index for k in existing), default=0) + 1)
    keyframe = ShotKeyframe(
        shot_id=shot.id,
        media_id=payload.media_id,
        order_index=order_index,
        label=payload.label or f"关键帧 {order_index}",
        prompt=payload.prompt,
        is_primary=payload.is_primary or not existing,
    )
    db.add(keyframe)
    await db.flush()
    if keyframe.is_primary:
        await set_primary(db, shot.id, keyframe.id)
    await db.commit()
    await db.refresh(keyframe)
    return await serialize_keyframe(db, keyframe)


@router.patch("/{shot_id}/keyframes/{keyframe_id}", response_model=ShotKeyframeRead)
async def update_keyframe(
    shot_id: int,
    keyframe_id: int,
    payload: ShotKeyframeUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShotKeyframeRead:
    shot = await _get_owned_shot(db, user.id, shot_id)
    keyframe = await _get_owned_keyframe(db, shot, keyframe_id)

    values = payload.model_dump(exclude_unset=True)
    if values.get("media_id") is not None:
        asset = await db.get(MediaAsset, int(values["media_id"]))
        if not asset or asset.user_id != user.id or asset.kind != "image":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="关键帧素材无效")

    make_primary = values.pop("is_primary", None)
    for field, value in values.items():
        setattr(keyframe, field, value)
    if make_primary:
        await set_primary(db, shot.id, keyframe.id)
    await db.commit()
    await db.refresh(keyframe)
    return await serialize_keyframe(db, keyframe)


@router.delete(
    "/{shot_id}/keyframes/{keyframe_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_keyframe(
    shot_id: int,
    keyframe_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除关键帧：记录与它专属的图片素材一起彻底删除（图还被别处引用时保留图）。"""
    shot = await _get_owned_shot(db, user.id, shot_id)
    keyframe = await _get_owned_keyframe(db, shot, keyframe_id)
    was_primary = keyframe.is_primary
    owned = [keyframe.media_id] if keyframe.media_id else []
    await db.delete(keyframe)
    await db.flush()
    if was_primary:
        # 主帧被删掉后，顺位把第一张补为主帧，避免图生视频没有默认首帧
        remaining = (await load_keyframes(db, [shot.id])).get(shot.id, [])
        if remaining:
            await set_primary(db, shot.id, remaining[0].id)
    paths = await purge_media(db, owned)
    await db.commit()
    remove_media_files(paths)


# ---------------------------------------------------------------- 生成
async def _project_id_of_shot(db: AsyncSession, shot: Shot) -> int | None:
    episode = await db.get(Episode, shot.episode_id)
    return episode.project_id if episode else None


async def _resolve_keyframe_target(
    db: AsyncSession, user: User, shot: Shot, keyframe_id: int | None
) -> int | None:
    """图片任务的落点：显式指定的关键帧 → 校验归属；否则交由 worker 新建一张。"""
    if keyframe_id is None:
        return None
    keyframe = await _get_owned_keyframe(db, shot, keyframe_id)
    return keyframe.id


@router.post("/{shot_id}/image", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def generate_shot_image(
    shot_id: int,
    payload: GenerationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    """生成关键帧：不传 keyframe_id 时新增一张，传了则重绘该张。"""
    shot = await _get_owned_shot(db, user.id, shot_id)
    keyframe_id = await _resolve_keyframe_target(db, user, shot, payload.keyframe_id)
    params = {
        "shot_id": shot.id,
        "credential_id": payload.credential_id,
        "provider": payload.provider,
        "model": payload.model,
        "prompt": payload.prompt,
        "size": payload.size,
        "aspect_ratio": payload.aspect_ratio,
        "n": payload.n,
        "keyframe_id": keyframe_id,
        "keyframe_label": payload.keyframe_label,
        "keyframe_prompts": payload.keyframe_prompts,
        "asset_ids": payload.asset_ids,
        "negative_prompt": payload.negative_prompt,
        "template_id": payload.template_id,
        "template_variables": payload.template_variables,
    }
    task = await create_task(
        db,
        user_id=user.id,
        kind="image",
        provider=payload.provider,
        model=payload.model,
        project_id=await _project_id_of_shot(db, shot),
        episode_id=shot.episode_id,
        shot_id=shot.id,
        params=params,
    )
    return TaskRead.model_validate(task)


@router.post("/{shot_id}/video", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def generate_shot_video(
    shot_id: int,
    payload: GenerationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    """基于关键帧生成分镜视频（图生视频）；默认取主关键帧作为首帧。"""
    shot = await _get_owned_shot(db, user.id, shot_id)

    image_media_id = payload.image_media_id
    if image_media_id:
        asset = await db.get(MediaAsset, int(image_media_id))
        if not asset or asset.user_id != user.id or asset.kind != "image":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="关键帧素材无效")
    else:
        keyframes = (await load_keyframes(db, [shot.id])).get(shot.id, [])
        primary = primary_keyframe(keyframes)
        image_media_id = primary.media_id if primary else None

    params = {
        "shot_id": shot.id,
        "credential_id": payload.credential_id,
        "provider": payload.provider,
        "model": payload.model,
        "prompt": payload.prompt,
        "duration": payload.duration,
        # 用户没显式选分辨率时沿用拆解阶段 AI 为这个镜头定的那一档
        "resolution": payload.resolution or shot.resolution,
        "aspect_ratio": payload.aspect_ratio,
        "image_media_id": image_media_id,
        "asset_ids": payload.asset_ids,
        "negative_prompt": payload.negative_prompt,
        "template_id": payload.template_id,
        "template_variables": payload.template_variables,
    }
    task = await create_task(
        db,
        user_id=user.id,
        kind="video",
        provider=payload.provider,
        model=payload.model,
        project_id=await _project_id_of_shot(db, shot),
        episode_id=shot.episode_id,
        shot_id=shot.id,
        params=params,
    )
    return TaskRead.model_validate(task)
