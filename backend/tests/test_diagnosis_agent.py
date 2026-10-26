"""失败诊断 agent：只读排障、按需重跑、审计落库。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.models.agent_run import AgentRun
from app.services.agents import diagnosis
from app.services.providers.base import ProviderError


class _Session:
    def __init__(self, task) -> None:
        self.task = task
        self.added: list = []

    async def get(self, model, pk):  # noqa: ANN001 - 测试替身
        if getattr(model, "__name__", "") == "GenerationTask" or model.__name__ == "GenerationTask":
            return self.task
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _failed_task(**overrides):
    data = {
        "id": 501,
        "kind": "image",
        "provider": "agnes",
        "model": "agnes-image",
        "error": "401 Unauthorized: invalid api key",
        "project_id": 34,
        "shot_id": None,
        "episode_id": None,
        "params": {
            "prompt": "雨夜的旧街",
            "flow": "pipeline_images",
            "chain_next": 777,
            "run_id": 12,
            "credential_id": 3,
        },
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _patch_llm(monkeypatch, decisions):
    from app.services import generation

    class _Provider:
        def model_for(self, capability: str) -> str:
            return "fake-text"

    async def fake_resolve(*args, **kwargs):
        return SimpleNamespace(id=3, provider="agnes")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    monkeypatch.setattr(generation, "build_provider_from", lambda credential: _Provider())

    queue = iter(decisions)

    async def fake_generate(provider, prompt, **kwargs):
        return next(queue)

    monkeypatch.setattr(generation, "_generate_json", fake_generate)


def test_diagnosis_reports_without_retry(monkeypatch):
    _patch_llm(
        monkeypatch,
        [
            {"thought": "看任务", "action": "inspect_task", "args": {}},
            {"thought": "尝试重跑", "action": "retry_task", "args": {}},
            {
                "thought": "鉴权失败",
                "action": "report",
                "args": {
                    "category": "auth",
                    "confidence": "high",
                    "evidence": ["401 Unauthorized: invalid api key"],
                    "suggestion": "到密钥管理里重新填写 Agnes 的 API Key",
                    "retry_recommended": True,
                },
            },
        ],
    )
    session = _Session(_failed_task())

    result = asyncio.run(
        diagnosis.run_diagnosis(
            session, user_id=1, failed_task_id=501, allow_retry=False
        )
    )

    assert result["status"] == "diagnosed"
    assert result["retry_task_id"] is None
    assert result["diagnosis"]["category"] == "auth"
    assert result["diagnosis"]["category_label"] == "密钥无效或无权限"
    assert result["diagnosis"]["retry_recommended"] is True
    denied = [step for step in result["trace"] if step["action"] == "retry_task"]
    assert denied and denied[0]["ok"] is False
    run = session.added[-1]
    assert isinstance(run, AgentRun)
    assert run.kind == "diagnose"
    assert run.target_type == "task"
    assert run.target_id == 501
    assert run.summary["diagnosis"]["category"] == "auth"


def test_retry_creates_independent_task_without_chain(monkeypatch):
    _patch_llm(
        monkeypatch,
        [
            {"thought": "换凭证重跑", "action": "retry_task", "args": {}},
            {
                "thought": "已重跑",
                "action": "report",
                "args": {
                    "category": "quota",
                    "confidence": "medium",
                    "evidence": ["429 rate limit"],
                    "suggestion": "等 1 分钟后查看重跑任务",
                    "retry_recommended": True,
                },
            },
        ],
    )
    created: dict = {}

    async def fake_create_task(db, **kwargs):
        created.update(kwargs)
        return SimpleNamespace(id=999)

    import app.services.dispatch as dispatch

    monkeypatch.setattr(dispatch, "create_task", fake_create_task)

    session = _Session(_failed_task(error="429 Too Many Requests"))
    result = asyncio.run(
        diagnosis.run_diagnosis(
            session,
            user_id=1,
            failed_task_id=501,
            allow_retry=True,
            retry_credential_id=8,
        )
    )

    assert result["status"] == "retried"
    assert result["retry_task_id"] == 999
    assert created["kind"] == "image"
    assert created["provider"] == "agnes"
    assert created["params"]["credential_id"] == 3
    assert created["params"]["diagnosis_of"] == 501
    assert "chain_next" not in created["params"]
    assert "flow" not in created["params"]
    assert "run_id" not in created["params"]


def test_missing_text_credential_returns_review_unavailable(monkeypatch):
    from app.services import generation

    async def fake_resolve(*args, **kwargs):
        raise ProviderError("未找到可用于「text」生成的 API Key")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)

    session = _Session(_failed_task())
    result = asyncio.run(diagnosis.run_diagnosis(session, user_id=1, failed_task_id=501))

    assert result["status"] == "review_unavailable"
    assert result["diagnosis"]["category"] == "credential_missing"
    assert result["steps"] == 0
    assert session.added[-1].status == "review_unavailable"


def test_unknown_category_is_coerced(monkeypatch):
    _patch_llm(
        monkeypatch,
        [
            {
                "thought": "看不懂",
                "action": "report",
                "args": {
                    "category": "alien_invasion",
                    "confidence": "whatever",
                    "evidence": [],
                    "suggestion": "查看任务详情",
                },
            },
        ],
    )
    session = _Session(_failed_task())
    result = asyncio.run(diagnosis.run_diagnosis(session, user_id=1, failed_task_id=501))

    assert result["diagnosis"]["category"] == "unknown"
    assert result["diagnosis"]["confidence"] == "medium"
