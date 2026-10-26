"""生成式金镜头回归：当前编译器出图→判官→落 eval 表，且不碰生产关键帧。

外部边界（厂商出图、落盘、视觉判官）用假实现替身，数据库和编译链路是真实的。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.episode import Episode
from app.models.eval_run import EvalRun, EvalRunItem
from app.models.keyframe import ShotKeyframe
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.shot import Shot
from app.models.user import User
from app.services import eval_runs
from app.services.eval_runs import run_golden_regression
from app.services.providers.base import ProviderError

pytestmark = pytest.mark.asyncio

SNAPSHOT_SOURCE = Path(__file__).parent / "fixtures" / "prompt_snapshots" / "p49_s212_image.json"


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _write_snapshot(tmp_path: Path, *, project_id: int = 1, shot_id: int = 901) -> Path:
    data = json.loads(SNAPSHOT_SOURCE.read_text(encoding="utf-8"))
    data["project_id"] = project_id
    data["shot_id"] = shot_id
    directory = tmp_path / "snapshots"
    directory.mkdir()
    (directory / f"p{project_id}_s{shot_id}_image.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return directory


async def _seed(session):
    user = User(username="eval-user", hashed_password="x")
    session.add(user)
    await session.flush()
    project = Project(user_id=user.id, title="评测项目", aspect_ratio="16:9")
    session.add(project)
    await session.flush()
    episode = Episode(project_id=project.id, title="第一集")
    session.add(episode)
    await session.flush()
    shot = Shot(
        id=901,
        episode_id=episode.id,
        order_index=1,
        title="测试镜头",
        description="小满坐在站牌下",
        image_prompt="小满坐在木质长椅上，低头看成绩单",
        duration_seconds=5.0,
        status="ready",
    )
    session.add(shot)
    await session.flush()
    approved = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source_asset = MediaAsset(
        user_id=user.id,
        project_id=project.id,
        shot_id=shot.id,
        kind="image",
        filename="source.png",
        path="/tmp/source.png",
        url="/media/source.png",
        meta={
            "reference_images": ["https://cdn.example/ref-a.png"],
            "reference_roles": [{"kind": "actor", "name": "小满"}],
        },
    )
    session.add(source_asset)
    await session.flush()
    keyframe = ShotKeyframe(
        shot_id=shot.id,
        media_id=source_asset.id,
        order_index=1,
        is_primary=True,
        approved_at=approved,
    )
    session.add(keyframe)
    await session.commit()
    return user, project, shot, keyframe, approved


def _stub_external(monkeypatch, captured: dict):
    import app.services.agents.visual_qa as visual_qa
    import app.services.generation as generation
    import app.services.storage as storage

    class FakeProvider:
        async def generate_image(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["refs"] = kwargs.get("reference_image_urls")
            return SimpleNamespace(
                url="https://cdn.example/generated.png",
                data=b"fake-bytes",
                mime_type="image/png",
                meta={"model": "agnes-image-2.5-flash"},
            )

    async def fake_resolve(session, user_id, capability, credential_id=None, provider_key=None):
        return SimpleNamespace(provider="agnes")

    async def fake_persist(session, **kwargs):
        asset = MediaAsset(
            user_id=kwargs["user_id"],
            project_id=kwargs.get("project_id"),
            shot_id=kwargs.get("shot_id"),
            kind=kwargs["kind"],
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            filename="eval.png",
            path="/tmp/eval.png",
            url="/media/eval.png",
            meta=kwargs.get("meta"),
        )
        session.add(asset)
        await session.flush()
        captured["asset_id"] = asset.id
        return asset

    async def fake_review(session, *, user_id, asset, shot):
        captured["judged_asset"] = asset.id
        return {"verdict": "pass", "checks": {"composition": "pass"}, "reasons": []}

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    monkeypatch.setattr(generation, "build_provider_from", lambda credential: FakeProvider())
    monkeypatch.setattr(storage, "persist_media", fake_persist)
    monkeypatch.setattr(visual_qa, "review_keyframe", fake_review)


async def test_generate_mode_produces_isolated_candidate_and_report(db_session, tmp_path, monkeypatch):
    user, project, shot, keyframe, approved = await _seed(db_session)
    monkeypatch.setattr(eval_runs, "FIXTURE_DIR", _write_snapshot(tmp_path))
    captured: dict = {}
    _stub_external(monkeypatch, captured)

    outcome = await run_golden_regression(
        db_session, user_id=user.id, project_id=project.id, mode="generate"
    )

    assert outcome["status"] == "completed"
    assert outcome["mode"] == "generate"
    assert outcome["totals"]["pass"] == 1
    # 送进厂商的是当前编译器拼出的提示词（冻结输入的正文 + 运行时块），参考图沿用生产那批
    snapshot = json.loads(
        (tmp_path / "snapshots" / "p1_s901_image.json").read_text(encoding="utf-8")
    )
    assert snapshot["inputs"]["base"][:12] in captured["prompt"]
    assert "【参考图" in captured["prompt"]
    assert captured["refs"] == ["https://cdn.example/ref-a.png"]

    run = (await db_session.execute(select(EvalRun))).scalar_one()
    assert run.kind == "golden_generate"
    item = (await db_session.execute(select(EvalRunItem))).scalar_one()
    assert item.verdict == "pass"
    assert item.media_id == captured["asset_id"]
    assert captured["judged_asset"] == captured["asset_id"]

    generated = await db_session.get(MediaAsset, captured["asset_id"])
    assert generated.project_id is None and generated.shot_id is None
    assert generated.meta["eval_isolated"] is True
    assert generated.meta["golden_shot_id"] == shot.id
    assert generated.meta["eval_run_id"] == run.id

    # 生产关键帧的认可状态和数量都不能被回归碰
    stored = await db_session.get(ShotKeyframe, keyframe.id)
    assert stored.approved_at == approved
    assert len((await db_session.execute(select(ShotKeyframe))).scalars().all()) == 1


async def test_generate_mode_records_provider_failure_without_losing_run(
    db_session, tmp_path, monkeypatch
):
    user, project, _, _, _ = await _seed(db_session)
    monkeypatch.setattr(eval_runs, "FIXTURE_DIR", _write_snapshot(tmp_path))

    import app.services.generation as generation

    async def fake_resolve(session, user_id, capability, credential_id=None, provider_key=None):
        return SimpleNamespace(provider="agnes")

    class FailingProvider:
        async def generate_image(self, prompt, **kwargs):
            raise ProviderError("Agnes 图生图队列已满（503）")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    monkeypatch.setattr(generation, "build_provider_from", lambda credential: FailingProvider())

    outcome = await run_golden_regression(
        db_session, user_id=user.id, project_id=project.id, mode="generate"
    )

    assert outcome["status"] == "completed"
    assert outcome["totals"]["error"] == 1
    item = (await db_session.execute(select(EvalRunItem))).scalar_one()
    assert "503" in (item.error or "")
    # run 本身必须留下来，否则失败轮次在历史里消失
    run = (await db_session.execute(select(EvalRun))).scalar_one()
    assert run.status == "completed"
