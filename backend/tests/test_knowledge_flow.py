"""教训库全链路：沉淀→待认可→认可→画风桶→使用打点→过期→复活。

这些是「具体情境」测试：直接操作数据库里的真实条目，走的是修复 agent 实际调用的
lookup / mark_used / stale_sweep / confirm，而不是只测纯函数。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.knowledge import KnowledgeNote
from app.models.user import User
from app.services.knowledge import confirm, lookup, mark_used, stale_sweep

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _user(session) -> User:
    user = User(username="wiki-user", hashed_password="x")
    session.add(user)
    await session.flush()
    return user


def _note(user: User, *, title: str, visual_style: str | None = None, approved: bool = False) -> KnowledgeNote:
    now = datetime.now(timezone.utc)
    return KnowledgeNote(
        user_id=user.id,
        scope="global",
        category="heuristic",
        title=title,
        content=f"{title} 的具体做法",
        preconditions={"media_type": "image"},
        visual_style=visual_style,
        status="active",
        approved_at=now if approved else None,
        last_confirmed_at=now if approved else None,
    )


async def test_note_stays_invisible_until_human_approves(db_session):
    user = await _user(db_session)
    note = _note(user, title="新沉淀的教训")
    db_session.add(note)
    await db_session.commit()

    found = await lookup(
        db_session, user_id=user.id, context={"media_type": "image"}
    )
    assert found == [], "未认可的条目绝不能被 agent 检索到"

    note.approved_at = datetime.now(timezone.utc)
    note.last_confirmed_at = note.approved_at
    await db_session.commit()
    found = await lookup(
        db_session, user_id=user.id, context={"media_type": "image"}
    )
    assert [item.id for item in found] == [note.id]

    # 前置条件不成立的场景仍然拿不到（视频教训不注入图片修复）
    assert await lookup(db_session, user_id=user.id, context={"media_type": "video"}) == []


async def test_style_tag_only_matches_same_style_project(db_session):
    user = await _user(db_session)
    tagged = _note(user, title="日系治愈专用", visual_style="日系治愈", approved=True)
    untagged = _note(user, title="通用教训", approved=True)
    db_session.add_all([tagged, untagged])
    await db_session.commit()

    healing = await lookup(
        db_session,
        user_id=user.id,
        visual_style="日系手绘动画质感，日系治愈短片",
        context={"media_type": "image"},
    )
    assert {item.title for item in healing} == {"日系治愈专用", "通用教训"}

    suspense = await lookup(
        db_session,
        user_id=user.id,
        visual_style="写实摄影，悬疑氛围",
        context={"media_type": "image"},
    )
    assert {item.title for item in suspense} == {"通用教训"}

    # 项目没有画风信息时，带标签的条目也不能碰运气注入
    unknown = await lookup(db_session, user_id=user.id, context={"media_type": "image"})
    assert {item.title for item in unknown} == {"通用教训"}


async def test_used_note_survives_sweep_and_idle_note_goes_stale_then_revives(db_session):
    user = await _user(db_session)
    used = _note(user, title="最近被用过的", approved=True)
    idle = _note(user, title="一百天没动的", approved=True)
    old = datetime.now(timezone.utc) - timedelta(days=100)
    db_session.add_all([used, idle])
    await db_session.commit()
    # 把两条都做成 100 天前创建，只有 used 保留最近使用时间
    for note in (used, idle):
        note.created_at = old
        note.last_confirmed_at = None
        note.last_used_at = None
    await db_session.commit()

    await mark_used(db_session, [used.id])
    await db_session.commit()
    swept = await stale_sweep(db_session)
    await db_session.commit()
    assert swept == 1
    assert (await db_session.get(KnowledgeNote, used.id)).status == "active"
    stale = await db_session.get(KnowledgeNote, idle.id)
    assert stale.status == "stale"
    found = await lookup(db_session, user_id=user.id, context={"media_type": "image"})
    assert [item.title for item in found] == ["最近被用过的"]

    # 人工确认后复活：stale 不是删除，结论还能被重新采用
    revived = await confirm(db_session, idle.id)
    await db_session.commit()
    assert revived is not None and revived.status == "active"
    found = await lookup(db_session, user_id=user.id, context={"media_type": "image"})
    assert {item.title for item in found} == {"最近被用过的", "一百天没动的"}


async def test_repair_agent_lookup_gate_uses_project_style(db_session):
    """修复 agent 的真实检索入口：项目画风不符时，带标签的教训进不了 system prompt。"""
    from app.services.agents.repair import _lookup_lessons, _lessons_block

    user = await _user(db_session)
    tagged = _note(user, title="治愈片专用结论", visual_style="日系治愈", approved=True)
    db_session.add(tagged)
    await db_session.commit()

    healing = await _lookup_lessons(
        db_session,
        user_id=user.id,
        project_id=None,
        media_type="image",
        visual_style="日系手绘动画质感，日系治愈短片",
    )
    block = _lessons_block(healing)
    assert tagged.title in block
    assert "【生产教训" in block

    suspense = await _lookup_lessons(
        db_session,
        user_id=user.id,
        project_id=None,
        media_type="image",
        visual_style="写实摄影，悬疑氛围",
    )
    assert suspense == []
