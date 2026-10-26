"""生产教训库的检索与生命周期：资格门硬过滤，approved_at 为空绝不返回。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNote

STALE_AFTER_DAYS = 90


def _matches(note: KnowledgeNote, context: dict) -> bool:
    """资格门：preconditions 里的每个键值都必须在上下文中成立。"""
    for key, value in (note.preconditions or {}).items():
        if context.get(key) != value:
            return False
    return True


def _style_matches(note: KnowledgeNote, visual_style: str | None) -> bool:
    """画风桶：带标签的条目只注入画风文本包含该标签的项目。

    项目没有画风信息时只注入无标签条目——宁可少注入，也不能把某一种
    画风的教训塞进完全不同的项目。
    """
    tag = (note.visual_style or "").strip()
    if not tag:
        return True
    if not visual_style:
        return False
    return tag.casefold() in visual_style.casefold()


async def lookup(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int | None = None,
    visual_style: str | None = None,
    context: dict | None = None,
    category: str | None = None,
) -> list[KnowledgeNote]:
    """返回可注入的条目：已认可 + active + scope 匹配 + 前置条件成立 + 画风匹配。

    条目不足百条之前就是标签过滤，不引向量检索。
    """
    scope_values = ["global"] if project_id is None else ["global", "project"]
    stmt = select(KnowledgeNote).where(
        KnowledgeNote.user_id == user_id,
        KnowledgeNote.status == "active",
        KnowledgeNote.approved_at.is_not(None),
        KnowledgeNote.scope.in_(scope_values),
    )
    if project_id is not None:
        stmt = stmt.where(
            (KnowledgeNote.project_id == project_id) | (KnowledgeNote.project_id.is_(None))
        )
    if category:
        stmt = stmt.where(KnowledgeNote.category == category)
    rows = (await session.execute(stmt.order_by(KnowledgeNote.id))).scalars().all()
    context = context or {}
    if project_id is not None:
        context = {"project_id": project_id, **context}
    return [
        note
        for note in rows
        if _matches(note, context) and _style_matches(note, visual_style)
    ]


async def mark_used(session: AsyncSession, note_ids: list[int]) -> None:
    """注入方顺手打点：这条知识最近一次真的被用上是什么时候。失败不影响主流程。"""
    if not note_ids:
        return
    try:
        await session.execute(
            update(KnowledgeNote)
            .where(KnowledgeNote.id.in_(note_ids))
            .values(last_used_at=datetime.now(timezone.utc))
        )
    except Exception:  # noqa: BLE001 - 打点是旁路
        await session.rollback()


async def confirm(session: AsyncSession, note_id: int) -> KnowledgeNote | None:
    note = await session.get(KnowledgeNote, note_id)
    if note is None:
        return None
    now = datetime.now(timezone.utc)
    note.last_confirmed_at = now
    if note.status == "stale":
        note.status = "active"
    return note


async def supersede(
    session: AsyncSession, old_id: int, new_id: int
) -> KnowledgeNote | None:
    """取代链：旧条目 archived + 留指针；只应由人工在新结论确认核心推翻后调用。"""
    old = await session.get(KnowledgeNote, old_id)
    new = await session.get(KnowledgeNote, new_id)
    if old is None or new is None or old.id == new.id:
        return None
    old.status = "archived"
    old.superseded_by = new.id
    return old


async def stale_sweep(session: AsyncSession) -> int:
    """cron 用：太久没被确认也没被使用的条目降为 stale（不删除，可人工复活）。"""
    threshold = datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)
    result = await session.execute(
        update(KnowledgeNote)
        .where(
            KnowledgeNote.status == "active",
            KnowledgeNote.approved_at.is_not(None),
            KnowledgeNote.created_at < threshold,
            (KnowledgeNote.last_confirmed_at.is_(None))
            | (KnowledgeNote.last_confirmed_at < threshold),
            (KnowledgeNote.last_used_at.is_(None)) | (KnowledgeNote.last_used_at < threshold),
        )
        .values(status="stale")
    )
    return int(result.rowcount or 0)
