"""分集：详情 / 更新 / 删除 / 分镜列表与创建 / 剧本拆解。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.serialize import serialize_shots
from app.db.session import get_db
from app.models.episode import Episode
from app.models.project import Project
from app.models.shot import Shot
from app.models.user import User
from app.schemas.content import (
    EpisodeRead,
    EpisodeUpdate,
    ShotCreate,
    ShotPlanRead,
    ShotPlanRequest,
    ShotRead,
)
from app.schemas.task import BreakdownRequest, TaskRead
from app.services import generation
from app.services.dispatch import create_task
from app.services.references import (
    media_of_shots,
    purge_media,
    remove_media_files,
    shots_of_episodes,
)

router = APIRouter(prefix="/episodes", tags=["分集"])


async def _get_owned_episode(db: AsyncSession, user_id: int, episode_id: int) -> Episode:
    episode = await db.get(Episode, episode_id)
    if not episode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分集不存在")
    project = await db.get(Project, episode.project_id)
    if not project or project.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分集不存在")
    return episode


async def _shot_count(db: AsyncSession, episode_id: int) -> int:
    return int(
        (
            await db.execute(
                select(func.count(Shot.id)).where(Shot.episode_id == episode_id)
            )
        ).scalar_one()
    )


@router.get("/{episode_id}", response_model=EpisodeRead)
async def get_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeRead:
    episode = await _get_owned_episode(db, user.id, episode_id)
    item = EpisodeRead.model_validate(episode)
    item.shot_count = await _shot_count(db, episode.id)
    return item


@router.patch("/{episode_id}", response_model=EpisodeRead)
async def update_episode(
    episode_id: int,
    payload: EpisodeUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeRead:
    episode = await _get_owned_episode(db, user.id, episode_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(episode, field, value)
    await db.commit()
    await db.refresh(episode)
    item = EpisodeRead.model_validate(episode)
    item.shot_count = await _shot_count(db, episode.id)
    return item


@router.delete("/{episode_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除分集：其下分镜的关键帧图片与视频若不再被别处引用，一并彻底删除。"""
    episode = await _get_owned_episode(db, user.id, episode_id)
    shot_ids = await shots_of_episodes(db, [episode.id])
    owned = await media_of_shots(db, shot_ids)
    await db.delete(episode)
    await db.flush()
    paths = await purge_media(db, owned)
    await db.commit()
    remove_media_files(paths)


@router.get("/{episode_id}/shots", response_model=list[ShotRead])
async def list_shots(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ShotRead]:
    episode = await _get_owned_episode(db, user.id, episode_id)
    shots = (
        await db.execute(
            select(Shot)
            .where(Shot.episode_id == episode.id)
            .order_by(Shot.order_index, Shot.id)
        )
    ).scalars().all()
    return await serialize_shots(db, list(shots))


@router.post("/{episode_id}/shots", response_model=ShotRead, status_code=status.HTTP_201_CREATED)
async def create_shot(
    episode_id: int,
    payload: ShotCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShotRead:
    episode = await _get_owned_episode(db, user.id, episode_id)

    order_index = payload.order_index
    if order_index is None:
        max_index = (
            await db.execute(
                select(func.coalesce(func.max(Shot.order_index), 0)).where(
                    Shot.episode_id == episode.id
                )
            )
        ).scalar_one()
        order_index = int(max_index) + 1

    shot = Shot(
        episode_id=episode.id,
        order_index=order_index,
        title=payload.title,
        description=payload.description,
        dialogue=payload.dialogue,
        camera=payload.camera,
        duration_seconds=payload.duration_seconds,
        image_prompt=payload.image_prompt,
        video_prompt=payload.video_prompt,
        directives=payload.directives,
        subjects=[item.model_dump(mode="json") for item in payload.subjects] if payload.subjects else None,
        props=[item.model_dump(mode="json") for item in payload.props] if payload.props else None,
        action_timeline=(
            [item.model_dump(mode="json") for item in payload.action_timeline]
            if payload.action_timeline
            else None
        ),
        start_state=payload.start_state,
        end_state=payload.end_state,
        status="pending",
    )
    db.add(shot)
    await db.commit()
    await db.refresh(shot)
    shots = await serialize_shots(db, [shot])
    return shots[0]


@router.post("/{episode_id}/breakdown-plan", response_model=ShotPlanRead)
async def breakdown_plan_episode(
    episode_id: int,
    payload: ShotPlanRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShotPlanRead:
    """拆解前先要一份规划建议（同步返回，不改分集、不删镜头）。

    用户看完理由和分段再决定是否拆解，避免「一次 AI 直接把整集拆歪」。
    """
    episode = await _get_owned_episode(db, user.id, episode_id)
    data = await generation.suggest_shot_plan(
        db,
        user_id=user.id,
        episode_id=episode.id,
        shot_count=payload.shot_count,
        target_seconds=payload.target_seconds,
        style=payload.style,
        credential_id=payload.credential_id,
        provider_key=payload.provider,
        model=payload.model,
    )
    return ShotPlanRead(**data)


@router.post("/{episode_id}/breakdown", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def breakdown_episode(
    episode_id: int,
    payload: BreakdownRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    """调用大模型把剧本拆解为分镜（异步任务）。"""
    episode = await _get_owned_episode(db, user.id, episode_id)
    if payload.shot_count is None and not (payload.plan or {}).get("beats"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先点「AI 规划拆解方案」确认镜头数，或手动填写镜头数量",
        )
    params = {
        "action": "breakdown",
        "episode_id": episode.id,
        "credential_id": payload.credential_id,
        "provider": payload.provider,
        "model": payload.model,
        "shot_count": payload.shot_count,
        "target_seconds": payload.target_seconds,
        "style": payload.style,
        "skills": payload.skills,
        "plan": payload.plan,
        "auto_images": payload.auto_images,
    }
    task = await create_task(
        db,
        user_id=user.id,
        kind="text",
        provider=payload.provider,
        model=payload.model,
        project_id=episode.project_id,
        episode_id=episode.id,
        params=params,
    )
    return TaskRead.model_validate(task)
