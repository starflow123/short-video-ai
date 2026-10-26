"""导演自检 agent：确定性校验、修订收敛与审计落库。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.models.agent_run import AgentRun
from app.services.agents import director


class _Session:
    """只覆盖 record_run 用到的 add / flush，不连库。"""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _plan(beats: list[dict], *, shots: int = 8, seconds: int = 8, target: int = 64) -> dict:
    return {
        "reasoning": "按冲突节奏拆分",
        "shot_count": shots,
        "shot_seconds": seconds,
        "target_seconds": target,
        "beats": beats,
    }


def test_validate_plan_reports_structural_breakage():
    issues = director.validate_plan(
        _plan([{"name": "开场", "shots": 3, "seconds": 24, "summary": "起"}]),
        script="完整的剧本内容",
        shot_count=8,
        shot_seconds=8,
        target_seconds=64,
    )
    codes = {item["code"] for item in issues}
    assert "shot_sum" in codes
    assert "beat_count_low" in codes
    assert "duration_sum" in codes
    assert any(item["level"] == "critical" for item in issues)


def test_validate_plan_accepts_balanced_plan():
    issues = director.validate_plan(
        _plan(
            [
                {"name": "开场钩子", "shots": 3, "seconds": 24, "summary": "起"},
                {"name": "冲突升级", "shots": 3, "seconds": 24, "summary": "承"},
                {"name": "反转收束", "shots": 2, "seconds": 16, "summary": "合"},
            ]
        ),
        script="完整的剧本内容",
        shot_count=8,
        shot_seconds=8,
        target_seconds=64,
    )
    assert issues == []


def test_empty_script_is_critical():
    issues = director.validate_plan(
        _plan([]),
        script="",
        shot_count=8,
        shot_seconds=8,
        target_seconds=64,
    )
    assert {item["code"] for item in issues} >= {"script_empty", "beats_empty"}


def test_coerce_plan_rescales_to_exact_shot_count():
    raw = {
        "reasoning": "三段",
        "beats": [
            {"name": "开场", "shots": 5, "seconds": 0, "summary": "起"},
            {"name": "冲突", "shots": 5, "seconds": 0, "summary": "承"},
            {"name": "收束", "shots": 5, "seconds": 0, "summary": "合"},
        ],
    }
    current = _plan([])
    merged = director._coerce_plan(raw, current, shot_count=8, shot_seconds=8, target_seconds=64)
    assert sum(int(beat["shots"]) for beat in merged["beats"]) == 8
    assert all(int(beat["seconds"]) > 0 for beat in merged["beats"])
    assert merged["shot_count"] == 8


def test_clean_plan_does_not_call_model_and_records_run():
    session = _Session()
    episode = SimpleNamespace(id=11, project_id=22, title="第一集")

    result = asyncio.run(
        director.review_plan(
            session,
            user_id=1,
            episode=episode,
            plan=_plan(
                [
                    {"name": "开场钩子", "shots": 3, "seconds": 24, "summary": "起"},
                    {"name": "冲突升级", "shots": 3, "seconds": 24, "summary": "承"},
                    {"name": "反转收束", "shots": 2, "seconds": 16, "summary": "合"},
                ]
            ),
            script="完整的剧本内容",
            shot_count=8,
            shot_seconds=8,
            target_seconds=64,
        )
    )

    assert result["status"] == "clean"
    assert result["steps"] == 0
    assert result["issues"] == []
    assert len(session.added) == 1
    run = session.added[0]
    assert isinstance(run, AgentRun)
    assert run.kind == "director"
    assert run.status == "clean"


def test_broken_plan_enters_bounded_revision_loop(monkeypatch):
    from app.services import generation

    class _Provider:
        def model_for(self, capability: str) -> str:
            return "fake-text"

    async def fake_resolve(*args, **kwargs):
        return SimpleNamespace(provider="fake")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    monkeypatch.setattr(generation, "build_provider_from", lambda credential: _Provider())

    decisions = iter(
        [
            {"thought": "先校验", "action": "validate", "args": {}},
            {"thought": "按问题修一版", "action": "revise", "args": {}},
            {"thought": "修完了", "action": "finish", "args": {"status": "revised", "summary": "已修"}},
        ]
    )
    llm_calls = {"n": 0}

    async def fake_generate(provider, prompt, **kwargs):
        if "请修订下面这份分镜拆解方案" in prompt:
            llm_calls["n"] += 1
            return {
                "reasoning": "补足段落",
                "beats": [
                    {"name": "开场钩子", "shots": 3, "seconds": 24, "summary": "起"},
                    {"name": "冲突升级", "shots": 3, "seconds": 24, "summary": "承"},
                    {"name": "反转收束", "shots": 2, "seconds": 16, "summary": "合"},
                ],
            }
        return next(decisions)

    monkeypatch.setattr(generation, "_generate_json", fake_generate)

    session = _Session()
    result = asyncio.run(
        director.review_plan(
            session,
            user_id=1,
            episode=SimpleNamespace(id=11, project_id=22, title="第一集"),
            plan=_plan([{"name": "开场", "shots": 3, "seconds": 24, "summary": "起"}]),
            script="完整的剧本内容",
            shot_count=8,
            shot_seconds=8,
            target_seconds=64,
        )
    )

    assert llm_calls["n"] == 1
    assert result["status"] == "revised"
    assert result["revisions"] == 1
    assert result["issues"] == []
    assert [step["action"] for step in result["trace"]] == ["validate", "revise", "finish"]
    assert sum(int(beat["shots"]) for beat in result["plan"]["beats"]) == 8
    assert session.added[0].status == "revised"
