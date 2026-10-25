"""项目：CRUD + 分集创建 + 成片导出。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.asset import ProductionAsset
from app.models.episode import Episode
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.shot import Shot
from app.models.task import GenerationTask
from app.models.user import User
from app.schemas.content import (
    EpisodeCreate,
    EpisodeRead,
    ProjectCreate,
    ProjectRead,
    ProjectSuggestRead,
    ProjectSuggestRequest,
    ProjectUpdate,
)
from app.schemas.task import ForgeRequest, TaskRead
from app.services import generation
from app.services.dispatch import create_task
from app.services.references import (
    assets_of_projects,
    detach_asset,
    media_of_assets,
    media_of_shots,
    media_paths_of,
    project_media_ids,
    purge_media,
    remove_media_files,
    shots_of_projects,
)

router = APIRouter(prefix="/projects", tags=["项目"])


async def _get_owned(db: AsyncSession, user_id: int, project_id: int) -> Project:
    project = await db.get(Project, project_id)
    if not project or project.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return project


async def _counts(
    db: AsyncSession, project_ids: list[int]
) -> tuple[dict[int, int], dict[int, dict[str, int]]]:
    """返回：每项目 episode 数 + 每项目镜头状态分桶 {pending, ready, running, total}。"""
    if not project_ids:
        return {}, {}
    episode_rows = (
        await db.execute(
            select(Episode.project_id, func.count(Episode.id))
            .where(Episode.project_id.in_(project_ids))
            .group_by(Episode.project_id)
        )
    ).all()
    # 按 project_id + shot.status 分组
    shot_rows = (
        await db.execute(
            select(Episode.project_id, Shot.status, func.count(Shot.id))
            .join(Shot, Shot.episode_id == Episode.id)
            .where(Episode.project_id.in_(project_ids))
            .group_by(Episode.project_id, Shot.status)
        )
    ).all()
    episode_counts = {row[0]: row[1] for row in episode_rows}
    buckets: dict[int, dict[str, int]] = {}
    for pid, status, cnt in shot_rows:
        b = buckets.setdefault(pid, {"pending": 0, "ready": 0, "running": 0, "total": 0})
        b["total"] += int(cnt)
        if status in ("pending", "failed"):
            b["pending"] += int(cnt)
        elif status in ("prepared", "ready"):
            b["ready"] += int(cnt)
        elif status == "generating":
            b["running"] += int(cnt)
    return episode_counts, buckets


def _fill_stats(item: ProjectRead, bucket: dict[str, int] | None) -> None:
    b = bucket or {"pending": 0, "ready": 0, "running": 0, "total": 0}
    total = b["total"]
    item.shot_count = total
    item.shots_pending = b["pending"]
    item.shots_ready = b["ready"]
    item.shots_running = b["running"]
    item.progress = int(round(b["ready"] * 100 / total)) if total else 0


@router.get("", response_model=list[ProjectRead])
async def list_projects(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ProjectRead]:
    projects = (
        await db.execute(
            select(Project).where(Project.user_id == user.id).order_by(Project.id.desc())
        )
    ).scalars().all()
    episode_counts, shot_buckets = await _counts(db, [p.id for p in projects])

    result: list[ProjectRead] = []
    for project in projects:
        item = ProjectRead.model_validate(project)
        item.episode_count = episode_counts.get(project.id, 0)
        _fill_stats(item, shot_buckets.get(project.id))
        result.append(item)
    return result


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectRead:
    project = Project(
        user_id=user.id,
        title=payload.title,
        description=payload.description,
        aspect_ratio=payload.aspect_ratio,
        style=payload.style,
        visual_style=payload.visual_style,
        video_style=payload.video_style,
        seed=payload.seed,
        style_lock=payload.style_lock,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return ProjectRead.model_validate(project)


@router.post("/suggest", response_model=ProjectSuggestRead)
async def suggest_project_endpoint(
    payload: ProjectSuggestRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectSuggestRead:
    """AI 智能补全：把粗糙想法补成标题 / 简介 / 风格 / 标签（同步返回）。"""
    data = await generation.suggest_project_setting(
        db,
        user_id=user.id,
        idea=payload.idea,
        aspect_ratio=payload.aspect_ratio,
        credential_id=payload.credential_id,
        provider_key=payload.provider,
        model=payload.model,
    )
    return ProjectSuggestRead(**data)


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectRead:
    project = await _get_owned(db, user.id, project_id)
    episode_counts, shot_buckets = await _counts(db, [project.id])
    item = ProjectRead.model_validate(project)
    item.episode_count = episode_counts.get(project.id, 0)
    _fill_stats(item, shot_buckets.get(project.id))
    return item


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: int,
    payload: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectRead:
    project = await _get_owned(db, user.id, project_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    await db.commit()
    await db.refresh(project)
    return ProjectRead.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除项目：连带清掉该项目独占的素材与资产（关键帧 / 分镜视频 / 成片 / 参考图）。

    全局资产要跨项目复用，保留资产行与参考图，只解除项目挂载；
    非全局资产随项目删除，并摘掉分镜里指向它的 asset_id，避免留下悬空引用。
    """
    project = await _get_owned(db, user.id, project_id)
    shot_ids = await shots_of_projects(db, [project.id])
    asset_ids = await assets_of_projects(db, [project.id])
    owned = [
        *await media_of_shots(db, shot_ids),
        *await media_of_assets(db, asset_ids),
        *await project_media_ids(db, [project.id]),
    ]
    # 素材行会被 projects.id 的 CASCADE 直接带走，路径必须在删宿主之前抢读
    preloaded = await media_paths_of(db, owned)

    project_assets = (
        await db.execute(
            select(ProductionAsset).where(
                ProductionAsset.user_id == user.id,
                ProductionAsset.project_id == project.id,
            )
        )
    ).scalars().all()
    local_assets = [asset for asset in project_assets if not asset.is_global]
    for asset in project_assets:
        if not asset.is_global:
            continue
        # 全局资产的参考图也挂在项目下，不先摘钩会随项目 CASCADE 一起删掉
        asset.project_id = None
        if asset.media_id:
            media = await db.get(MediaAsset, int(asset.media_id))
            if media and media.project_id == project.id:
                media.project_id = None

    if local_assets:
        await detach_asset(db, [asset.id for asset in local_assets])
        for asset in local_assets:
            await db.delete(asset)
    await db.flush()

    await db.delete(project)
    await db.flush()
    paths = await purge_media(db, owned, preloaded=preloaded)
    await db.commit()
    remove_media_files(paths)


@router.get("/{project_id}/episodes", response_model=list[EpisodeRead])
async def list_episodes(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EpisodeRead]:
    project = await _get_owned(db, user.id, project_id)
    episodes = (
        await db.execute(
            select(Episode)
            .where(Episode.project_id == project.id)
            .order_by(Episode.order_index, Episode.id)
        )
    ).scalars().all()

    shot_counts: dict[int, int] = {}
    if episodes:
        rows = (
            await db.execute(
                select(Shot.episode_id, func.count(Shot.id))
                .where(Shot.episode_id.in_([e.id for e in episodes]))
                .group_by(Shot.episode_id)
            )
        ).all()
        shot_counts = {row[0]: row[1] for row in rows}

    result: list[EpisodeRead] = []
    for episode in episodes:
        item = EpisodeRead.model_validate(episode)
        item.shot_count = shot_counts.get(episode.id, 0)
        result.append(item)
    return result


@router.post("/{project_id}/episodes", response_model=EpisodeRead, status_code=status.HTTP_201_CREATED)
async def create_episode(
    project_id: int,
    payload: EpisodeCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EpisodeRead:
    project = await _get_owned(db, user.id, project_id)

    order_index = payload.order_index
    if order_index is None:
        max_index = (
            await db.execute(
                select(func.coalesce(func.max(Episode.order_index), 0)).where(
                    Episode.project_id == project.id
                )
            )
        ).scalar_one()
        order_index = int(max_index) + 1

    episode = Episode(
        project_id=project.id,
        order_index=order_index,
        title=payload.title,
        synopsis=payload.synopsis,
        script_text=payload.script_text,
    )
    db.add(episode)
    await db.commit()
    await db.refresh(episode)
    return EpisodeRead.model_validate(episode)


@router.post("/{project_id}/forge", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def forge_project_endpoint(
    project_id: int,
    payload: ForgeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    """AI 一键生成：主题/世界观 → 剧本 → 分镜指令（异步任务）。"""
    project = await _get_owned(db, user.id, project_id)
    params = {
        "action": "forge",
        "premise": payload.premise,
        "style": payload.style,
        "shot_count": payload.shot_count,
        "target_seconds": payload.target_seconds,
        "extra": payload.extra,
        "credential_id": payload.credential_id,
        "provider": payload.provider,
        "model": payload.model,
        "skills": payload.skills,
    }
    task = await create_task(
        db,
        user_id=user.id,
        kind="text",
        provider=payload.provider,
        model=payload.model,
        project_id=project.id,
        params=params,
    )
    return TaskRead.model_validate(task)


@router.post("/{project_id}/export", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def export_project_endpoint(
    project_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    project = await _get_owned(db, user.id, project_id)
    # 连点「导出成片」不应该排队导出同一部片子两次
    existing = (
        await db.execute(
            select(GenerationTask)
            .where(
                GenerationTask.user_id == user.id,
                GenerationTask.project_id == project.id,
                GenerationTask.kind == "export",
                GenerationTask.status.in_(("pending", "running")),
            )
            .order_by(GenerationTask.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if existing:
        return TaskRead.model_validate(existing)
    task = await create_task(
        db, user_id=user.id, kind="export", project_id=project.id, params={}
    )
    return TaskRead.model_validate(task)
