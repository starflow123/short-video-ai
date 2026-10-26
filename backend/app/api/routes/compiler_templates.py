"""全局编译模板（RSI 受限面）：提案 / 认可 / 回滚 / 查看当前生效版本。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.compiler_template import CompilerTemplate
from app.models.user import User
from app.services.compiler_templates import approve, load_active, propose, rollback
from app.services.prompts import TEMPLATE_KEYS

router = APIRouter(prefix="/compiler-templates", tags=["编译模板"])


def _serialize(row: CompilerTemplate) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "content": row.content,
        "version": row.version,
        "is_active": row.is_active,
        "approved": row.approved_at is not None,
        "rationale": row.rationale,
        "validated": row.validated,
        "proposed_by_run_id": row.proposed_by_run_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class TemplatePropose(BaseModel):
    key: str
    content: str = Field(min_length=1)
    rationale: str | None = None
    proposed_by_run_id: int | None = None


@router.get("/keys")
async def list_keys(user: User = Depends(get_current_user)) -> dict:
    """白名单键与当前生效覆盖。角色档 / 分镜模板等项目级资产永远不在这里。"""
    from app.services.prompts import get_template_overrides

    return {"keys": list(TEMPLATE_KEYS), "active_overrides": get_template_overrides()}


@router.get("")
async def list_templates(
    key: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = select(CompilerTemplate).where(CompilerTemplate.user_id == user.id)
    if key:
        stmt = stmt.where(CompilerTemplate.key == key)
    rows = (
        await db.execute(stmt.order_by(CompilerTemplate.key, CompilerTemplate.version.desc()))
    ).scalars().all()
    return [_serialize(row) for row in rows]


@router.post("/propose", status_code=201)
async def propose_template(
    body: TemplatePropose,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row = await propose(
            db,
            user_id=user.id,
            key=body.key,
            content=body.content,
            rationale=body.rationale,
            proposed_by_run_id=body.proposed_by_run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return _serialize(row)


@router.post("/{template_id}/approve")
async def approve_template(
    template_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row = await approve(db, user_id=user.id, template_id=template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return _serialize(row)


@router.post("/rollback")
async def rollback_template(
    key: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        previous = await rollback(db, user_id=user.id, key=key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {
        "key": key,
        "active_content": (await load_active(db)).get(key),
        "rolled_back_to": previous.version if previous else None,
        "message": "已回滚" + (f"到 v{previous.version}" if previous else "到内置模板"),
    }
