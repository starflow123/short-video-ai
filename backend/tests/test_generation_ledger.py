"""台账写入：任务终态落 generation_ledger 一行，异常不影响任务本身。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.ledger import GenerationLedger
from app.models.task import GenerationTask
from app.models.user import User
from app.services.generation import _write_ledger
from app.services.prompts import compiler_version

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


async def _seed_task(session, *, started=True) -> int:
    user = User(username="ledger-user", hashed_password="x")
    session.add(user)
    await session.flush()
    task = GenerationTask(
        user_id=user.id,
        kind="image",
        provider="agnes",
        model="agnes-hd",
        status="succeeded",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc) + timedelta(seconds=12),
    )
    session.add(task)
    await session.flush()
    return task.id


async def test_write_ledger_records_terminal_state(db_session):
    task_id = await _seed_task(db_session)
    await _write_ledger(db_session, task_id, status="succeeded", result={"media_id": 7}, error=None)
    await db_session.commit()
    row = (
        await db_session.execute(select(GenerationLedger).where(GenerationLedger.task_id == task_id))
    ).scalar_one()
    assert row.status == "succeeded"
    assert row.user_id is not None
    assert row.kind == "image"
    assert row.provider == "agnes"
    assert row.media_id == 7
    assert row.duration_ms == 12000
    assert row.prompt_version == compiler_version()
    assert len(row.prompt_version) == 12


async def test_write_ledger_missing_task_is_silent(db_session):
    await _write_ledger(db_session, 9999, status="failed", result=None, error="boom")
    await db_session.commit()
    rows = (await db_session.execute(select(GenerationLedger))).scalars().all()
    assert rows == []


async def test_write_ledger_swallows_internal_errors(db_session, monkeypatch):
    task_id = await _seed_task(db_session)

    def fake_select(*args, **kwargs):
        raise RuntimeError("db hiccup")

    import app.services.generation as gen

    monkeypatch.setattr(gen, "select", fake_select)
    # 不抛异常即算过：台账是旁路能力，坏了只能跳过
    await _write_ledger(db_session, task_id, status="failed", result=None, error="x")
