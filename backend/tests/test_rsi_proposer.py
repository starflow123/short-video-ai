"""RSI 自动提案：从失败台账与诊断结论起草 patch，只能进待认可队列。

用 sqlite + 假模型替换 _draft，验证「汇总 → 起草 → 金镜头断言 → 未生效」整条链，
不联网、不碰真实供应商。
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.agent_run import AgentRun
from app.models.compiler_template import CompilerTemplate
from app.models.ledger import GenerationLedger
from app.models.project import Project
from app.models.user import User

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rsi_propose_from_diagnoses.py"
_spec = importlib.util.spec_from_file_location("rsi_propose_from_diagnoses", _SCRIPT)
rsi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsi)


class _KeepOpen:
    """main 里的 `async with compiler_session()`：换成测试会话且不关闭。"""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):  # noqa: ANN002 - 上下文协议
        return False


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(session, *, failures: int = 3) -> tuple[int, int]:
    user = User(username="rsi-user", hashed_password="x")
    session.add(user)
    await session.flush()
    project = Project(user_id=user.id, title="rsi-project")
    session.add(project)
    await session.flush()
    for index in range(failures):
        session.add(
            GenerationLedger(
                task_id=1000 + index,
                user_id=user.id,
                project_id=project.id,
                kind="video",
                provider="agnes",
                model="agnes-video",
                status="failed",
                error="Agnes 视频队列已满（503），重试 14 次",
            )
        )
    session.add(
        AgentRun(
            user_id=user.id,
            project_id=project.id,
            kind="diagnose",
            status="diagnosed",
            steps=2,
            summary={
                "diagnosis": {
                    "category": "quota",
                    "evidence": ["503 队列已满"],
                    "suggestion": "等厂商额度恢复后再重试",
                }
            },
        )
    )
    await session.commit()
    return user.id, project.id


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        key="video_continuity_compact",
        days=14,
        project=None,
        user=None,
        credential_id=None,
        provider=None,
        model=None,
        min_failures=1,
        dry_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_draft_lands_in_pending_queue_not_active(db_session, monkeypatch):
    user_id, project_id = await _seed(db_session)

    async def fake_draft(*_args, **kwargs):
        assert kwargs["user_id"] == user_id
        return {
            "content": "【前后镜头衔接】上一镜收尾动作直接顺接，人物位置与朝向不跳变。",
            "rationale": "3 条视频任务因队列满失败，收紧衔接措辞以减少重试。",
        }

    monkeypatch.setattr(rsi, "_draft", fake_draft)
    monkeypatch.setattr(rsi, "compiler_session", lambda: _KeepOpen(db_session))

    exit_code = await rsi.main(_args(user=user_id, project=project_id))
    assert exit_code == 0

    row = (
        await db_session.execute(select(CompilerTemplate))
    ).scalar_one()
    assert row.key == "video_continuity_compact"
    assert row.version == 1
    assert row.is_active is False
    assert row.approved_at is None
    assert row.validated and row.validated["checked"] >= 16
    assert "队列满" in (row.rationale or "")


@pytest.mark.asyncio
async def test_unsafe_patch_is_rejected_and_nothing_is_queued(db_session, monkeypatch):
    user_id, project_id = await _seed(db_session)

    async def fake_draft(*_args, **kwargs):
        return {"content": "blurry, low quality", "rationale": "测试不安全的负面词"}

    monkeypatch.setattr(rsi, "_draft", fake_draft)
    monkeypatch.setattr(rsi, "compiler_session", lambda: _KeepOpen(db_session))

    exit_code = await rsi.main(
        _args(user=user_id, project=project_id, key="image_negative_compact")
    )
    assert exit_code == 1
    rows = (await db_session.execute(select(CompilerTemplate))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_model_abstains_when_evidence_does_not_fit(db_session, monkeypatch):
    user_id, project_id = await _seed(db_session)

    async def fake_draft(*_args, **kwargs):
        return {"content": "", "rationale": "失败都是队列限流，与衔接模板无关。"}

    monkeypatch.setattr(rsi, "_draft", fake_draft)
    monkeypatch.setattr(rsi, "compiler_session", lambda: _KeepOpen(db_session))

    exit_code = await rsi.main(_args(user=user_id, project=project_id))
    assert exit_code == 0
    assert (await db_session.execute(select(CompilerTemplate))).scalars().all() == []


@pytest.mark.asyncio
async def test_too_few_samples_skips_before_calling_model(db_session, monkeypatch):
    user_id, project_id = await _seed(db_session, failures=1)

    async def must_not_run(*_args, **kwargs):
        raise AssertionError("样本不足时不应该调用模型")

    monkeypatch.setattr(rsi, "_draft", must_not_run)
    monkeypatch.setattr(rsi, "compiler_session", lambda: _KeepOpen(db_session))

    exit_code = await rsi.main(
        _args(user=user_id, project=project_id, min_failures=3)
    )
    assert exit_code == 0


def test_term_normalization_strips_header_and_folds_newlines():
    assert rsi._norm_terms("【负面提示】deformed, text\nwatermark") == (
        "deformed, text, watermark"
    )
    assert rsi._norm_content("video_continuity_compact", " 【前后镜头衔接】保持。 ") == (
        "【前后镜头衔接】保持。"
    )


def test_error_signature_scrubs_secrets_and_ids():
    sig = rsi._signature("key sk-abcdef1234567890 任务 12345 失败")
    assert "sk-abcdef" not in sig
    assert "12345" not in sig
    assert "任务 # 失败" in sig
