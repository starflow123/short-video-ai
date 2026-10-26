"""任务中心：列表 / 详情 / 取消 / 重试。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent_run import AgentRun
from app.models.task import GenerationTask
from app.models.user import User
from app.schemas.task import (
    CancelActiveRead,
    DiagnoseRequest,
    DiagnosisRead,
    TaskBatchDeleteIn,
    TaskBatchDeleteRead,
    TaskRead,
)
from app.services.dispatch import create_task
from app.services.generation import resume_after_cancel
from app.services.references import detach_task
from app.worker.celery_app import celery_app

router = APIRouter(prefix="/tasks", tags=["任务"])


async def _get_owned_task(db: AsyncSession, user_id: int, task_id: int) -> GenerationTask:
    task = await db.get(GenerationTask, task_id)
    if not task or task.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return task


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    project_id: int | None = Query(default=None),
    episode_id: int | None = Query(default=None),
    shot_id: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    task_status: str | None = Query(default=None, alias="status"),
    orphan: bool = Query(default=False, description="只看未关联项目的任务（如全局资产出图）"),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[TaskRead]:
    stmt = select(GenerationTask).where(GenerationTask.user_id == user.id)
    if orphan:
        stmt = stmt.where(GenerationTask.project_id.is_(None))
    elif project_id is not None:
        stmt = stmt.where(GenerationTask.project_id == project_id)
    if episode_id is not None:
        stmt = stmt.where(GenerationTask.episode_id == episode_id)
    if shot_id is not None:
        stmt = stmt.where(GenerationTask.shot_id == shot_id)
    if kind:
        stmt = stmt.where(GenerationTask.kind == kind)
    if task_status:
        stmt = stmt.where(GenerationTask.status == task_status)

    tasks = (
        await db.execute(stmt.order_by(GenerationTask.id.desc()).limit(limit))
    ).scalars().all()
    return [TaskRead.model_validate(task) for task in tasks]


@router.post("/cancel-active", response_model=CancelActiveRead)
async def cancel_active_tasks(
    project_id: int | None = Query(default=None),
    asset_id: int | None = Query(default=None),
    orphan: bool = Query(default=False, description="取消所有未关联项目的任务"),
    kind: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CancelActiveRead:
    """按范围批量取消排队中 / 执行中的任务。

    资产（尤其是不属于任何项目的全局资产）出图任务不会出现在项目分组里，
    以前只能逐条取消；这里提供按项目、按资产或「未关联项目」整批取消。
    """
    stmt = select(GenerationTask).where(
        GenerationTask.user_id == user.id,
        GenerationTask.status.in_(("pending", "running")),
    )
    if orphan:
        stmt = stmt.where(GenerationTask.project_id.is_(None))
    elif project_id is not None:
        stmt = stmt.where(GenerationTask.project_id == project_id)
    if kind:
        stmt = stmt.where(GenerationTask.kind == kind)

    rows = list((await db.execute(stmt.order_by(GenerationTask.id.desc()))).scalars().all())
    if asset_id is not None:
        # asset_id 存在 params JSON 里，统一按字符串比较，避免 int/str 类型差异
        rows = [
            task
            for task in rows
            if str((task.params or {}).get("asset_id") or "") == str(asset_id)
        ]

    for task in rows:
        if task.celery_task_id:
            try:
                celery_app.control.revoke(task.celery_task_id, terminate=True)
            except Exception:  # noqa: BLE001 - 撤销失败不影响标记取消
                pass
        task.status = "canceled"
    await db.commit()
    # 链式任务取消后要推进到下一环，否则后续镜头会永远停在 pending
    for task in rows:
        await resume_after_cancel(db, task)
    return CancelActiveRead(canceled=len(rows), task_ids=[task.id for task in rows])


@router.post("/delete-batch", response_model=TaskBatchDeleteRead)
async def delete_tasks_batch(
    payload: TaskBatchDeleteIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskBatchDeleteRead:
    """批量删除任务记录：只删已结束的任务，进行中的跳过并回传，由前端提示先取消。

    路径必须放在 ``/{task_id}`` 之前，否则会被当成 id 解析。
    """
    rows = (
        await db.execute(
            select(GenerationTask).where(
                GenerationTask.user_id == user.id,
                GenerationTask.id.in_(payload.task_ids),
            )
        )
    ).scalars().all()
    active = [row.id for row in rows if row.status in ("pending", "running")]
    finished = [row for row in rows if row.status not in ("pending", "running")]

    if finished:
        ids = [row.id for row in finished]
        # 先解除素材对任务的引用，再删记录，避免 media_assets.task_id 变成悬空外键
        await detach_task(db, ids)
        for row in finished:
            await db.delete(row)
        await db.commit()

    return TaskBatchDeleteRead(
        deleted=len(finished),
        skipped_ids=active,
    )


@router.post("/delete-all", response_model=TaskBatchDeleteRead)
async def delete_all_tasks(
    project_id: int | None = Query(default=None),
    orphan: bool = Query(default=False, description="删除所有未关联项目的任务记录"),
    kind: str | None = Query(default=None),
    task_status: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskBatchDeleteRead:
    """按范围清空任务记录：只删已结束的，进行中的原样保留并回传。

    与 ``delete-batch`` 的区别是不依赖前端已加载的那一页，直接按范围在库里取，
    所以「共 400 条但只加载了 100 条」时也能一次清干净。
    """
    stmt = select(GenerationTask).where(GenerationTask.user_id == user.id)
    if orphan:
        stmt = stmt.where(GenerationTask.project_id.is_(None))
    elif project_id is not None:
        stmt = stmt.where(GenerationTask.project_id == project_id)
    if kind:
        stmt = stmt.where(GenerationTask.kind == kind)
    if task_status:
        stmt = stmt.where(GenerationTask.status == task_status)

    rows = list((await db.execute(stmt)).scalars().all())
    active = [row.id for row in rows if row.status in ("pending", "running")]
    finished = [row for row in rows if row.status not in ("pending", "running")]

    if finished:
        ids = [row.id for row in finished]
        await detach_task(db, ids)
        for row in finished:
            await db.delete(row)
        await db.commit()

    return TaskBatchDeleteRead(deleted=len(finished), skipped_ids=active)


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task = await _get_owned_task(db, user.id, task_id)
    return TaskRead.model_validate(task)


@router.post("/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task = await _get_owned_task(db, user.id, task_id)
    if task.status in ("succeeded", "failed", "canceled"):
        return TaskRead.model_validate(task)

    if task.celery_task_id:
        try:
            celery_app.control.revoke(task.celery_task_id, terminate=True)
        except Exception:  # noqa: BLE001 - 撤销失败不影响标记取消
            pass

    task.status = "canceled"
    await db.commit()
    # 链式任务被取消后如果不推进，后续镜头会永远停在 pending。
    # 这里继续推进剩余链；被取消的镜头没有视频时，导出会被完整性检查挡住并明确报错。
    await resume_after_cancel(db, task)
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除任务记录：仅允许删除已结束的任务，进行中的任务需先取消。"""
    task = await _get_owned_task(db, user.id, task_id)
    if task.status in ("pending", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="任务仍在进行中，请先取消再删除"
        )
    # 解除素材对这条任务的引用（素材本身保留在素材库），避免 media_assets.task_id 悬空
    await detach_task(db, [task_id])
    await db.delete(task)
    await db.commit()


@router.post("/{task_id}/retry", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def retry_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    task = await _get_owned_task(db, user.id, task_id)
    if task.status in ("pending", "running"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="任务仍在进行中，无法重试")

    new_task = await create_task(
        db,
        user_id=user.id,
        kind=task.kind,
        provider=task.provider,
        model=task.model,
        project_id=task.project_id,
        episode_id=task.episode_id,
        shot_id=task.shot_id,
        params=dict(task.params or {}),
    )
    return TaskRead.model_validate(new_task)


@router.post("/{task_id}/diagnose", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def diagnose_task(
    task_id: int,
    payload: DiagnoseRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    """给失败任务挂一条诊断 agent；原任务状态与分镜状态都不变。"""
    task = await _get_owned_task(db, user.id, task_id)
    if task.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="只有失败的任务才需要诊断"
        )
    new_task = await create_task(
        db,
        user_id=user.id,
        kind="diagnose",
        project_id=task.project_id,
        episode_id=task.episode_id,
        shot_id=task.shot_id,
        params={
            "failed_task_id": task.id,
            "allow_retry": payload.allow_retry,
            "credential_id": payload.credential_id,
            "provider": payload.provider,
            "model": payload.model,
            "retry_credential_id": payload.retry_credential_id,
            "retry_provider": payload.retry_provider,
            "retry_model": payload.retry_model,
        },
    )
    return TaskRead.model_validate(new_task)


@router.get("/{task_id}/diagnosis", response_model=DiagnosisRead)
async def get_diagnosis(
    task_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DiagnosisRead:
    """读取失败任务最近一次诊断结论（只读，不触发新调用）。"""
    await _get_owned_task(db, user.id, task_id)
    run = (
        await db.execute(
            select(AgentRun)
            .where(
                AgentRun.user_id == user.id,
                AgentRun.kind == "diagnose",
                AgentRun.target_type == "task",
                AgentRun.target_id == task_id,
            )
            .order_by(AgentRun.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if run is None:
        return DiagnosisRead(task_id=task_id, found=False)
    return DiagnosisRead(
        task_id=task_id,
        found=True,
        status=run.status,
        model=run.model,
        steps=run.steps,
        summary=run.summary,
        trace=run.trace or [],
        error=run.error,
        created_at=run.created_at,
    )
