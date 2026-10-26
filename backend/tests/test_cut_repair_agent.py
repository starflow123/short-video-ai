"""切点修复 agent：只给建议、按需重拍、结论落审计。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.models.agent_run import AgentRun
from app.services.agents import cut_repair


class _Session:
    def __init__(self, shot, episode=None) -> None:
        self.shot = shot
        self.episode = episode
        self.added: list = []

    async def get(self, model, pk):  # noqa: ANN001 - 测试替身
        name = getattr(model, "__name__", "")
        if name == "Shot":
            return self.shot
        if name == "Episode":
            return self.episode
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _review(verdict: str = "fail") -> dict:
    return {
        "verdict": verdict,
        "checks": {
            "same_person": "pass",
            "costume_continuity": "fail",
            "scene_continuity": "pass",
            "lighting_continuity": "warn",
            "keyframe_match": "pass",
        },
        "reasons": ["下一镜杨间的校服颜色变成浅灰"],
        "from_shot_id": 1,
        "to_shot_id": 2,
        "advisory": True,
    }


def _shot(shot_id: int = 2, episode_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        id=shot_id,
        episode_id=episode_id,
        order_index=shot_id,
        title="教室冲突",
        description="杨间穿黑色校服",
        directives={},
        subjects=[{"name": "杨间", "costume": "黑色校服"}],
        video_prompt="杨间在教室门口停住",
    )


def _patch_env(monkeypatch, decisions):
    from app.services import generation
    from app.services.agents import continuity

    class _Provider:
        def model_for(self, capability: str) -> str:
            return "fake-text"

    async def fake_resolve(*args, **kwargs):
        return SimpleNamespace(id=4, provider="deepseek")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    monkeypatch.setattr(generation, "build_provider_from", lambda credential: _Provider())

    queue = iter(decisions)

    async def fake_generate(provider, prompt, **kwargs):
        return next(queue)

    monkeypatch.setattr(generation, "_generate_json", fake_generate)

    async def fake_load_review(session, *, user_id, shot, force=False):
        return SimpleNamespace(id=1), _review()

    monkeypatch.setattr(cut_repair, "_load_cut_review", fake_load_review)


def test_patch_only_records_audit(monkeypatch):
    _patch_env(
        monkeypatch,
        [
            {"thought": "写修正", "action": "patch_prompt", "args": {"patch": "开场第一秒保持黑色校服"}},
            {
                "thought": "只给建议",
                "action": "report",
                "args": {"summary": "按修正重跑下一镜视频"},
            },
        ],
    )
    session = _Session(_shot())
    result = asyncio.run(
        cut_repair.run_cut_repair(
            session, user_id=1, shot_id=2, task_id=88, allow_rerender=False
        )
    )

    assert result["status"] == "patch_only"
    assert result["patch"] == "开场第一秒保持黑色校服"
    assert result["rerendered"] is False
    assert result["before"]["verdict"] == "fail"
    run = session.added[-1]
    assert isinstance(run, AgentRun)
    assert run.kind == "cut_repair"
    assert run.target_type == "shot"
    assert run.target_id == 2
    assert run.summary["patch"] == "开场第一秒保持黑色校服"


def test_rerender_is_denied_when_not_allowed(monkeypatch):
    _patch_env(
        monkeypatch,
        [
            {"thought": "写修正", "action": "patch_prompt", "args": {"patch": "保持黑色校服"}},
            {"thought": "重拍", "action": "rerender_video", "args": {}},
            {"thought": "交建议", "action": "report", "args": {"summary": "请手动重跑"}},
        ],
    )
    session = _Session(_shot())
    result = asyncio.run(
        cut_repair.run_cut_repair(
            session, user_id=1, shot_id=2, task_id=88, allow_rerender=False
        )
    )

    assert result["status"] == "patch_only"
    denied = [step for step in result["trace"] if step["action"] == "rerender_video"]
    assert denied and denied[0]["ok"] is False
    assert "未开启重拍" in denied[0]["observation"]


def test_server_switch_blocks_rerender(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "cut_repair_rerender_enabled", False)
    _patch_env(
        monkeypatch,
        [
            {"thought": "写修正", "action": "patch_prompt", "args": {"patch": "保持黑色校服"}},
            {"thought": "重拍", "action": "rerender_video", "args": {}},
            {"thought": "交建议", "action": "report", "args": {"summary": "请手动重跑"}},
        ],
    )
    session = _Session(_shot())
    result = asyncio.run(
        cut_repair.run_cut_repair(
            session, user_id=1, shot_id=2, task_id=88, allow_rerender=True
        )
    )

    denied = [step for step in result["trace"] if step["action"] == "rerender_video"]
    assert denied and denied[0]["ok"] is False
    assert "服务端未开启" in denied[0]["observation"]
    assert result["status"] == "patch_only"


def test_already_pass_short_circuits(monkeypatch):
    from app.services import generation

    async def fake_resolve(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("审查通过时不应调用模型")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    session = _Session(_shot())

    async def fake_load_review(session, *, user_id, shot, force=False):
        return SimpleNamespace(id=1), _review("pass")

    monkeypatch.setattr(cut_repair, "_load_cut_review", fake_load_review)

    result = asyncio.run(
        cut_repair.run_cut_repair(session, user_id=1, shot_id=2, task_id=88)
    )

    assert result["status"] == "already_pass"
    run = session.added[-1]
    assert isinstance(run, AgentRun)
    assert run.kind == "cut_repair"
    assert run.status == "already_pass"
    assert run.target_id == 2
