"""agent 运行审计查询：只读，用于前端「AI 处理记录」与排障回看。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.agent_run import AgentRun
from app.models.user import User
from app.schemas.agent import AgentRunRead

router = APIRouter(prefix="/agent-runs", tags=["AI 处理记录"])


@router.get("", response_model=list[AgentRunRead])
async def list_agent_runs(
    project_id: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    target_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentRunRead]:
    stmt = select(AgentRun).where(AgentRun.user_id == user.id)
    if project_id is not None:
        stmt = stmt.where(AgentRun.project_id == project_id)
    if kind:
        stmt = stmt.where(AgentRun.kind == kind)
    if target_type:
        stmt = stmt.where(AgentRun.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AgentRun.target_id == target_id)
    rows = (
        await db.execute(stmt.order_by(AgentRun.id.desc()).limit(limit))
    ).scalars().all()
    return [AgentRunRead.model_validate(row) for row in rows]


@router.get("/{run_id}", response_model=AgentRunRead)
async def get_agent_run(
    run_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunRead:
    run = await db.get(AgentRun, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    return AgentRunRead.model_validate(run)
