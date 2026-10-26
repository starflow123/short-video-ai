"""素材整理 agent：确定性扫描、只读、建议排序。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from app.models.agent_run import AgentRun
from app.services.agents import organize


class _Result:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _Session:
    def __init__(self, *, assets=(), shots=(), images=(), media_ids=()) -> None:
        self.assets = list(assets)
        self.shots = list(shots)
        self.images = list(images)
        self.media_ids = list(media_ids)
        self.added: list = []

    async def execute(self, stmt):  # noqa: ANN001 - 测试替身
        desc = stmt.column_descriptions[0]
        entity = desc["entity"].__name__
        if entity == "ProductionAsset":
            return _Result(self.assets)
        if entity == "Shot":
            return _Result(self.shots)
        if entity == "MediaAsset":
            if desc.get("name") == "id":
                return _Result(self.media_ids)
            return _Result(self.images)
        raise AssertionError(f"未预期的查询：{entity}")

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _asset(asset_id, kind, name, *, media_id=None, prompt=None):
    return SimpleNamespace(
        id=asset_id, kind=kind, name=name, media_id=media_id, prompt=prompt, is_global=False
    )


def _shot(shot_id, *, subjects=None, props=None):
    return SimpleNamespace(id=shot_id, subjects=subjects or [], props=props or [])


def test_scan_finds_duplicates_missing_media_unknown_names_and_unlinked():
    session = _Session(
        assets=[
            _asset(1, "actor", "杨间", prompt="黑色校服"),
            _asset(2, "actor", "杨 间", prompt="黑色校服"),
            _asset(3, "scene", "旧街", media_id=999),
        ],
        shots=[
            _shot(10, subjects=[{"name": "杨间"}, {"name": "张伟"}], props=[{"name": "柴刀"}]),
            _shot(11, subjects=[{"name": "张伟"}]),
        ],
        images=[
            SimpleNamespace(
                id=100, shot_id=None, filename="old.png", created_at=datetime(2026, 10, 1)
            ),
            SimpleNamespace(
                id=101, shot_id=10, filename="used.png", created_at=datetime(2026, 10, 2)
            ),
        ],
        media_ids=[],
    )

    scan = asyncio.run(organize._scan(session, user_id=1, project_id=None))
    kinds = {item["kind"] for item in scan.suggestions}

    assert "duplicate_asset" in kinds
    assert "missing_media" in kinds
    assert "missing_prompt" in kinds
    assert "unknown_subject" in kinds
    assert "unknown_prop" in kinds
    assert "unlinked_media" in kinds
    dup = next(item for item in scan.suggestions if item["kind"] == "duplicate_asset")
    assert dup["target_ids"] == [1, 2]
    missing = next(
        item
        for item in scan.suggestions
        if item["kind"] == "missing_media" and item["target_ids"] == [3]
    )
    assert missing["severity"] == "high"
    unlinked = next(item for item in scan.suggestions if item["kind"] == "unlinked_media")
    assert unlinked["target_ids"] == [100]
    assert scan.data["counts"]["unknown_actor_names"] == 1
    assert scan.data["counts"]["unlinked_images"] == 1


def test_apply_order_keeps_all_items():
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert organize._apply_order(items, [3, 1]) == [{"id": 3}, {"id": 1}, {"id": 2}]
    assert organize._apply_order(items, []) == items


def test_clean_scan_does_not_call_model():
    session = _Session()

    result = asyncio.run(
        organize.organize_assets(session, user_id=1, project_id=None, use_llm=True)
    )

    assert result["status"] == "clean"
    assert result["suggestions"] == []
    assert result["steps"] == 0
    run = session.added[-1]
    assert isinstance(run, AgentRun)
    assert run.kind == "organize"
    assert run.status == "clean"


def test_deterministic_mode_returns_suggestions_without_model():
    session = _Session(
        assets=[_asset(1, "actor", "杨间"), _asset(2, "actor", "杨间")],
    )
    result = asyncio.run(
        organize.organize_assets(session, user_id=1, project_id=None, use_llm=False)
    )

    assert result["status"] == "suggested"
    assert result["suggestions"]
    assert result["steps"] == 0
    assert session.added[-1].status == "suggested"
