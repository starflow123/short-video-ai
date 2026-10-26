"""主动型 agent 入口：素材整理（只读扫描 + 可选优先级排序）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.agent import OrganizeRead, OrganizeRequest
from app.services.agents.organize import organize_assets

router = APIRouter(prefix="/agents", tags=["AI 助手"])


@router.post("/organize-assets", response_model=OrganizeRead)
async def organize_assets_route(
    payload: OrganizeRequest,
    project_id: int | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrganizeRead:
    """扫描素材库（重名资产 / 缺参考图 / 未建档角色 / 未关联图片），只给建议。"""
    try:
        result = await organize_assets(
            db,
            user_id=user.id,
            project_id=project_id,
            use_llm=payload.use_llm,
            credential_id=payload.credential_id,
            provider_key=payload.provider,
            model=payload.model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await db.commit()
    return OrganizeRead(**result)
