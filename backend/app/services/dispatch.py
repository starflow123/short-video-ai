"""任务落库与投递。"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import GenerationTask
from app.worker.tasks import enqueue_generation


async def create_task(
    db: AsyncSession,
    *,
    user_id: int,
    kind: str,
    provider: str | None = None,
    model: str | None = None,
    project_id: int | None = None,
    episode_id: int | None = None,
    shot_id: int | None = None,
    params: dict | None = None,
    enqueue: bool = True,
) -> GenerationTask:
    task = GenerationTask(
        user_id=user_id,
        kind=kind,
        provider=provider,
        model=model,
        project_id=project_id,
        episode_id=episode_id,
        shot_id=shot_id,
        params=params or {},
        status="pending",
        progress=0,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    if not enqueue:
        # 顺序链：先落库等前一个任务完成后由链路推进器投递
        return task

    await enqueue_task(db, task)
    return task


async def enqueue_task(db: AsyncSession, task: GenerationTask) -> None:
    """投递一个已落库的任务（顺序链推进时复用）。"""
    try:
        celery_id = enqueue_generation(task.id)
    except Exception as exc:  # noqa: BLE001 - 队列不可用时明确告知用户
        task.status = "failed"
        task.error = f"任务队列不可用：{exc}"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务队列不可用，请确认 Redis 与 Celery Worker 已启动",
        ) from exc

    task.celery_task_id = celery_id
    await db.commit()
    await db.refresh(task)
