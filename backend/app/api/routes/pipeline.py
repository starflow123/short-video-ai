"""制作流水线：建角色档案 → AI 出分镜 → 人工确认提示词 → 批量生图。

编排由 LangGraph 负责（``app.services.pipeline``），实际生图仍走 Celery 任务。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.episode import Episode
from app.models.keyframe import ShotKeyframe
from app.models.pipeline import PipelineRun
from app.models.project import Project
from app.models.shot import Shot
from app.models.user import User
from app.schemas.pipeline import (
    CharacterProfileRead,
    PipelineConfirmRequest,
    PipelineRunRead,
    PipelineStartRequest,
    PipelineVideoRequest,
)
from app.services import pipeline as pipeline_service
from app.services.dispatch import create_task
from app.services.providers.base import ProviderError

router = APIRouter(prefix="/pipeline", tags=["制作流水线"])


async def _get_owned_episode(db: AsyncSession, user_id: int, episode_id: int) -> Episode:
    episode = await db.get(Episode, episode_id)
    project = await db.get(Project, episode.project_id) if episode else None
    if not episode or not project or project.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="分集不存在")
    return episode


async def _get_owned_run(db: AsyncSession, user_id: int, run_id: int) -> PipelineRun:
    run = await db.get(PipelineRun, run_id)
    if not run or run.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="流水线记录不存在")
    return run


async def _stats(db: AsyncSession, episode_id: int) -> tuple[int, int, int, int]:
    """返回 (镜头总数, 已确认数, 已出图数, 已出视频数)。"""
    total = (
        await db.execute(
            select(func.count()).select_from(Shot).where(Shot.episode_id == episode_id)
        )
    ).scalar_one()
    confirmed = (
        await db.execute(
            select(func.count())
            .select_from(Shot)
            .where(Shot.episode_id == episode_id, Shot.prompt_confirmed.is_(True))
        )
    ).scalar_one()
    # 「已出图」按该镜头是否有关联图片的关键帧来算，避免依赖关键帧状态字段
    generated = (
        await db.execute(
            select(func.count(func.distinct(ShotKeyframe.shot_id)))
            .select_from(ShotKeyframe)
            .join(Shot, Shot.id == ShotKeyframe.shot_id)
            .where(Shot.episode_id == episode_id, ShotKeyframe.media_id.isnot(None))
        )
    ).scalar_one()
    videos = (
        await db.execute(
            select(func.count())
            .select_from(Shot)
            .where(Shot.episode_id == episode_id, Shot.video_media_id.isnot(None))
        )
    ).scalar_one()
    return int(total), int(confirmed), int(generated), int(videos)


async def _read_run(db: AsyncSession, run: PipelineRun) -> PipelineRunRead:
    total, confirmed, generated, videos = (
        await _stats(db, run.episode_id) if run.episode_id else (0, 0, 0, 0)
    )
    data = PipelineRunRead.model_validate(run)
    data.characters = [CharacterProfileRead(**item) for item in (run.characters or [])]
    data.total_shots = total
    data.confirmed_shots = confirmed
    data.generated_shots = generated
    data.generated_videos = videos
    # 图片任务全部落库后收敛状态（正常情况下由顺序链在链尾更新，这里是兜底）
    if run.status == "generating" and run.stage == "generate" and total > 0 and generated >= total:
        run.status = "images_ready"
        run.stage = "images_ready"
        await db.commit()
        data.status = run.status
        data.stage = run.stage
    return data


@router.get("/episode/{episode_id}", response_model=PipelineRunRead | None)
async def get_latest_run(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineRunRead | None:
    """取该分集最近一次流水线状态（用于进入分镜页时恢复进度）。"""
    await _get_owned_episode(db, user.id, episode_id)
    run = (
        await db.execute(
            select(PipelineRun)
            .where(PipelineRun.episode_id == episode_id, PipelineRun.user_id == user.id)
            .order_by(PipelineRun.id.desc())
            .limit(1)
        )
    ).scalars().first()
    return await _read_run(db, run) if run else None


@router.get("/runs/{run_id}", response_model=PipelineRunRead)
async def get_run(
    run_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineRunRead:
    run = await _get_owned_run(db, user.id, run_id)
    return await _read_run(db, run)


@router.post(
    "/episode/{episode_id}/start",
    response_model=PipelineRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_pipeline(
    episode_id: int,
    payload: PipelineStartRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineRunRead:
    """启动流水线：后台先建角色档案，再带档案出分镜，出完暂停等人工确认。"""
    episode = await _get_owned_episode(db, user.id, episode_id)
    if not (episode.script_text or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="分集剧本为空，请先填写剧本"
        )

    run = PipelineRun(
        user_id=user.id,
        project_id=episode.project_id,
        episode_id=episode.id,
        status="pending",
        stage="start",
        params=payload.model_dump(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    try:
        task = await create_task(
            db,
            user_id=user.id,
            kind="pipeline",
            provider=payload.provider,
            model=payload.model,
            project_id=episode.project_id,
            episode_id=episode.id,
            params={"run_id": run.id, "stage": "start"},
        )
    except HTTPException:
        run.status = "failed"
        run.error = "任务队列不可用，请确认 Redis 与 Celery Worker 已启动"
        await db.commit()
        raise
    run.task_id = task.id
    await db.commit()
    await db.refresh(run)
    return await _read_run(db, run)


@router.post("/runs/{run_id}/confirm", response_model=PipelineRunRead)
async def confirm_and_generate(
    run_id: int,
    payload: PipelineConfirmRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineRunRead:
    """人工确认后批量生图。

    - 传 ``shot_ids``：只确认并生成这些镜头（=「只重跑那个镜头」的批量版）；
    - 不传：把该分集所有镜头都标为已确认后批量生图。
    """
    run = await _get_owned_run(db, user.id, run_id)
    if not run.episode_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="流水线未关联分集")

    shots = list(
        (
            await db.execute(select(Shot).where(Shot.episode_id == run.episode_id))
        ).scalars().all()
    )
    if not shots:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该分集还没有分镜")

    if payload.shot_ids:
        wanted = {int(item) for item in payload.shot_ids}
        target_ids = {shot.id for shot in shots if shot.id in wanted}
        if not target_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="所选镜头不属于该分集")
    else:
        target_ids = {shot.id for shot in shots}
    for shot in shots:
        if shot.id in target_ids:
            shot.prompt_confirmed = True

    episode = await db.get(Episode, run.episode_id)
    if episode:
        episode.status = "generating"

    # 把本次确认的参数合并进运行记录，供批量生图节点读取
    run.params = {**(run.params or {}), **payload.model_dump(exclude_none=True)}
    run.params["shot_ids"] = sorted(target_ids)
    run.error = None
    await db.commit()

    try:
        await pipeline_service.run_pipeline(run.id, "confirmed")
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"批量生图失败：{exc}"
        ) from exc

    await db.refresh(run)
    return await _read_run(db, run)


@router.post("/runs/{run_id}/videos", response_model=PipelineRunRead)
async def batch_generate_videos(
    run_id: int,
    payload: PipelineVideoRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PipelineRunRead:
    """批量生视频：串行生成保证参考帧稳定，全部镜头齐了自动导出成片。

    - 传 ``shot_ids``：只重跑这些镜头；
    - 不传：补齐所有「已有关键帧、还没有视频」的镜头。
    """
    run = await _get_owned_run(db, user.id, run_id)
    if not run.episode_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="流水线未关联分集")

    shots = list(
        (
            await db.execute(select(Shot).where(Shot.episode_id == run.episode_id))
        ).scalars().all()
    )
    if not shots:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该分集还没有分镜")

    data = payload.model_dump(exclude_none=True)
    shot_ids = data.pop("shot_ids", None)
    if shot_ids:
        wanted = {int(item) for item in shot_ids}
        target_ids = {shot.id for shot in shots if shot.id in wanted}
        if not target_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="所选镜头不属于该分集")
    else:
        target_ids = None

    run.params = {**(run.params or {}), **data}
    if target_ids:
        run.params["video_shot_ids"] = sorted(target_ids)
    else:
        run.params.pop("video_shot_ids", None)
    run.error = None
    await db.commit()

    try:
        await pipeline_service.run_pipeline(run.id, "videos")
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"批量生视频失败：{exc}"
        ) from exc

    await db.refresh(run)
    return await _read_run(db, run)
