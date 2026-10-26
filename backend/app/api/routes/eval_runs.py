"""金镜头回归：手动触发与结果查看。只产报告，不碰主链路的认可状态。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.eval_run import EvalRun, EvalRunItem
from app.models.user import User
from app.services.eval_runs import run_golden_regression

router = APIRouter(prefix="/eval", tags=["金镜头回归"])


class EvalRunCreate(BaseModel):
    project_id: int
    # rejudge=审现有主关键帧（不花钱）；generate=按当前编译器重新出图再审（耗额度）
    mode: str = "rejudge"


@router.post("/runs")
async def create_eval_run(
    body: EvalRunCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await run_golden_regression(
            db, user_id=user.id, project_id=body.project_id, mode=body.mode
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
async def list_eval_runs(
    project_id: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = select(EvalRun).where(EvalRun.user_id == user.id)
    if project_id is not None:
        stmt = stmt.where(EvalRun.project_id == project_id)
    rows = (
        await db.execute(stmt.order_by(EvalRun.id.desc()).limit(limit))
    ).scalars().all()
    return [
        {
            "id": run.id,
            "project_id": run.project_id,
            "kind": run.kind,
            "status": run.status,
            "prompt_version": run.prompt_version,
            "totals": run.totals,
            "error": run.error,
            "created_at": run.created_at.isoformat() if run.created_at else None,
        }
        for run in rows
    ]


@router.get("/runs/{run_id}")
async def get_eval_run(
    run_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await db.get(EvalRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="回归记录不存在")
    items = (
        await db.execute(
            select(EvalRunItem).where(EvalRunItem.run_id == run_id).order_by(EvalRunItem.id)
        )
    ).scalars().all()
    return {
        "id": run.id,
        "project_id": run.project_id,
        "kind": run.kind,
        "status": run.status,
        "prompt_version": run.prompt_version,
        "totals": run.totals,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "items": [
            {
                "shot_id": item.shot_id,
                "media_id": item.media_id,
                "verdict": item.verdict,
                "prev_verdict": item.prev_verdict,
                "changed": item.changed,
                "checks": item.checks,
                "reasons": item.reasons,
                "error": item.error,
            }
            for item in items
        ],
    }
