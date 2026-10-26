"""生产教训库：认可制 CRUD + 资格门检索预览。AI 永远只能进待认可队列。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.knowledge import KnowledgeNote
from app.models.user import User
from app.services import knowledge as wiki

router = APIRouter(prefix="/knowledge", tags=["知识库"])


def _serialize(note: KnowledgeNote) -> dict:
    return {
        "id": note.id,
        "scope": note.scope,
        "project_id": note.project_id,
        "category": note.category,
        "title": note.title,
        "content": note.content,
        "preconditions": note.preconditions,
        "visual_style": note.visual_style,
        "status": note.status,
        "approved": note.approved_at is not None,
        "last_used_at": note.last_used_at.isoformat() if note.last_used_at else None,
        "last_confirmed_at": note.last_confirmed_at.isoformat()
        if note.last_confirmed_at
        else None,
        "superseded_by": note.superseded_by,
        "source_run_id": note.source_run_id,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }


class KnowledgeCreate(BaseModel):
    scope: str = Field(default="global", pattern="^(global|project)$")
    project_id: int | None = None
    category: str = Field(default="heuristic", pattern="^(fact|heuristic|style)$")
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    preconditions: dict | None = None
    visual_style: str | None = None
    source_run_id: int | None = None


@router.get("")
async def list_notes(
    include_unapproved: bool = Query(default=False),
    status_filter: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = select(KnowledgeNote).where(KnowledgeNote.user_id == user.id)
    if not include_unapproved:
        stmt = stmt.where(KnowledgeNote.approved_at.is_not(None))
    if status_filter:
        stmt = stmt.where(KnowledgeNote.status == status_filter)
    if category:
        stmt = stmt.where(KnowledgeNote.category == category)
    rows = (
        await db.execute(stmt.order_by(KnowledgeNote.id.desc()).limit(200))
    ).scalars().all()
    return [_serialize(note) for note in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_note(
    body: KnowledgeCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.scope == "project" and body.project_id is None:
        raise HTTPException(status_code=400, detail="project 条目必须指定 project_id")
    note = KnowledgeNote(
        user_id=user.id,
        project_id=body.project_id if body.scope == "project" else None,
        scope=body.scope,
        category=body.category,
        title=body.title,
        content=body.content,
        preconditions=body.preconditions or None,
        visual_style=body.visual_style,
        source_run_id=body.source_run_id,
        # 新条目一律先进待认可队列，approved_at 由人点亮
        status="active",
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return _serialize(note)


@router.post("/{note_id}/approve")
async def approve_note(
    note_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    note = await db.get(KnowledgeNote, note_id)
    if not note or note.user_id != user.id:
        raise HTTPException(status_code=404, detail="条目不存在")
    from datetime import datetime, timezone

    note.approved_at = datetime.now(timezone.utc)
    note.last_confirmed_at = note.approved_at
    await db.commit()
    return _serialize(note)


@router.post("/{note_id}/confirm")
async def confirm_note(
    note_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    note = await db.get(KnowledgeNote, note_id)
    if not note or note.user_id != user.id:
        raise HTTPException(status_code=404, detail="条目不存在")
    await wiki.confirm(db, note_id)
    await db.commit()
    return _serialize(note)


@router.post("/{note_id}/supersede")
async def supersede_note(
    note_id: int,
    by_id: int = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    for note_id_check in (note_id, by_id):
        note = await db.get(KnowledgeNote, note_id_check)
        if not note or note.user_id != user.id:
            raise HTTPException(status_code=404, detail="条目不存在")
    old = await wiki.supersede(db, note_id, by_id)
    if old is None:
        raise HTTPException(status_code=400, detail="取代关系不成立")
    await db.commit()
    return _serialize(old)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await db.get(KnowledgeNote, note_id)
    if not note or note.user_id != user.id:
        raise HTTPException(status_code=404, detail="条目不存在")
    await db.delete(note)
    await db.commit()


@router.get("/lookup")
async def lookup_notes(
    project_id: int | None = Query(default=None),
    visual_style: str | None = Query(default=None),
    category: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    media_type: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """资格门检索预览：注入方（agent / 编译器）实际拿到的就是这一条过滤结果。"""
    context: dict = {}
    if provider:
        context["provider"] = provider
    if media_type:
        context["media_type"] = media_type
    notes = await wiki.lookup(
        db,
        user_id=user.id,
        project_id=project_id,
        visual_style=visual_style,
        context=context,
        category=category,
    )
    return [_serialize(note) for note in notes]
