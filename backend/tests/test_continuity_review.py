"""切点连续性审查：提示词、结论归一化、抽帧命令与失败降级。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.agents import continuity


def _shot(shot_id: int, order: int, *, title: str, description: str, subjects=None):
    return SimpleNamespace(
        id=shot_id,
        episode_id=7,
        order_index=order,
        title=title,
        description=description,
        subjects=subjects or [],
        props=[],
        directives={"shot_size": "中景"},
        start_state=None,
        end_state=None,
        video_media_id=shot_id + 100,
    )


def test_cut_prompt_states_boundary_rules():
    prev = _shot(1, 1, title="推纸条", description="林越把纸条推给陈默")
    shot = _shot(
        2,
        2,
        title="警告",
        description="陈默盯着纸条",
        subjects=[{"name": "陈默", "costume": "浅灰卫衣"}],
    )

    prompt = continuity.build_cut_prompt(
        prev,
        shot,
        image_labels=["图1：上一镜最后一帧", "图2：下一镜第一帧"],
        has_keyframe=False,
    )

    assert "图1：上一镜最后一帧" in prompt
    assert "图2：下一镜第一帧" in prompt
    assert "第 1 镜《推纸条》" in prompt
    assert "第 2 镜《警告》" in prompt
    assert "姿态突变" in prompt and "一律不算问题" in prompt
    assert "明确换场" in prompt
    assert "没有提供图3" in prompt


def test_any_failed_check_makes_cut_fail():
    report = continuity.normalize_cut(
        {
            "verdict": "pass",
            "checks": {
                "same_person": "pass",
                "costume_continuity": "fail",
                "scene_continuity": "pass",
                "lighting_continuity": "pass",
                "keyframe_match": "pass",
            },
            "reasons": ["陈默的卫衣从浅灰变成了黑外套"],
        },
        provider="deepseek",
        model="deepseek-flash",
        from_media_id=10,
        to_media_id=11,
        from_shot_id=1,
        to_shot_id=2,
        has_keyframe=True,
    )

    assert report["verdict"] == "fail"
    assert report["checks"]["costume_continuity"] == "fail"
    assert report["from_media_id"] == 10
    assert report["to_media_id"] == 11
    assert report["advisory"] is True


def test_missing_keyframe_is_not_a_risk():
    report = continuity.normalize_cut(
        {
            "verdict": "pass",
            "checks": {
                "same_person": "pass",
                "costume_continuity": "pass",
                "scene_continuity": "pass",
                "lighting_continuity": "pass",
                "keyframe_match": "unknown",
            },
            "reasons": [],
        },
        provider="deepseek",
        model="deepseek-flash",
        from_media_id=1,
        to_media_id=2,
        from_shot_id=1,
        to_shot_id=2,
        has_keyframe=False,
    )

    assert report["checks"]["keyframe_match"] == "pass"
    assert report["verdict"] == "pass"


def test_unknown_keyframe_match_with_image_is_warn():
    report = continuity.normalize_cut(
        {
            "verdict": "pass",
            "checks": {
                "same_person": "pass",
                "costume_continuity": "pass",
                "scene_continuity": "pass",
                "lighting_continuity": "pass",
                "keyframe_match": "unknown",
            },
            "reasons": [],
        },
        provider="deepseek",
        model="deepseek-flash",
        from_media_id=1,
        to_media_id=2,
        from_shot_id=1,
        to_shot_id=2,
        has_keyframe=True,
    )

    assert report["verdict"] == "warn"


def test_empty_cut_treats_person_checks_as_not_applicable():
    report = continuity.normalize_cut(
        {
            "verdict": "warn",
            "checks": {
                "same_person": "unknown",
                "costume_continuity": "unknown",
                "scene_continuity": "pass",
                "lighting_continuity": "pass",
                "keyframe_match": "pass",
            },
            "reasons": [],
        },
        provider="deepseek",
        model="deepseek-flash",
        from_media_id=1,
        to_media_id=2,
        from_shot_id=1,
        to_shot_id=2,
        has_keyframe=True,
        subject_count=0,
    )

    assert report["checks"]["same_person"] == "pass"
    assert report["checks"]["costume_continuity"] == "pass"
    assert report["verdict"] == "pass"


def test_extract_frame_seeks_from_file_end_for_tail(monkeypatch):
    calls: list[tuple] = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"\x89PNG\r\n\x1a\nframe", b""

    async def _fake_exec(*cmd, **kwargs):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(continuity.asyncio, "create_subprocess_exec", _fake_exec)

    head = asyncio.run(continuity._extract_frame(continuity.Path("a.mp4"), at_end=False))
    tail = asyncio.run(continuity._extract_frame(continuity.Path("a.mp4"), at_end=True))

    assert head.startswith(b"\x89PNG")
    assert tail.startswith(b"\x89PNG")
    assert "-ss" in calls[0] and "-sseof" not in calls[0]
    assert "-sseof" in calls[1]


def test_extract_frame_without_ffmpeg_raises(monkeypatch):
    async def _missing(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(continuity.asyncio, "create_subprocess_exec", _missing)

    try:
        asyncio.run(continuity._extract_frame(continuity.Path("a.mp4"), at_end=False))
    except continuity.ProviderError as exc:
        assert "ffmpeg" in str(exc)
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("缺少 ffmpeg 时应抛出 ProviderError")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, shots):
        self.shots = shots
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _stmt):
        return _Result([(shot, None) for shot in self.shots])

    async def get(self, _model, pk):
        return next((shot for shot in self.shots if shot.id == pk), None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def test_review_project_keeps_going_after_one_cut_fails(monkeypatch):
    shots = [
        _shot(1, 1, title="A", description="a"),
        _shot(2, 2, title="B", description="b"),
        _shot(3, 3, title="C", description="c"),
    ]
    session = _Session(shots)

    async def _video_asset(_session, shot):
        return SimpleNamespace(id=shot.id + 100, meta={})

    calls: list[tuple[int, int]] = []

    async def _review_cut(_session, *, user_id, prev_shot, shot, force=False):
        calls.append((prev_shot.id, shot.id))
        if prev_shot.id == 1:
            raise RuntimeError("model timeout")
        return {
            "verdict": "pass",
            "checks": {},
            "reasons": [],
            "from_shot_id": prev_shot.id,
            "to_shot_id": shot.id,
        }

    monkeypatch.setattr(continuity, "_video_asset", _video_asset)
    monkeypatch.setattr(continuity, "review_cut", _review_cut)

    summary = asyncio.run(
        continuity.review_project(session, user_id=1, project_id=7)
    )

    assert calls == [(1, 2), (2, 3)]
    assert summary["cuts"] == 1
    assert summary["verdicts"]["pass"] == 1
    assert summary["skipped"][0]["from_shot_id"] == 1
    assert session.rollbacks == 1


def test_switch_off_makes_no_review_call(monkeypatch):
    async def _boom(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("开关关闭时不应调用审查")

    monkeypatch.setattr(continuity.settings, "continuity_review_enabled", False)
    monkeypatch.setattr(continuity, "review_project", _boom)
    result = asyncio.run(continuity.maybe_review_project(None, user_id=1, project_id=2))
    assert result is None


def test_auto_review_swallows_errors(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("vision down")

    monkeypatch.setattr(continuity.settings, "continuity_review_enabled", True)
    monkeypatch.setattr(continuity, "review_project", _boom)
    result = asyncio.run(
        continuity.maybe_review_project(_Session([]), user_id=1, project_id=2)
    )
    assert result is None


def test_export_runs_continuity_before_concat(monkeypatch):
    from app.services import generation

    calls: list[str] = []

    async def _review(_session, *, user_id, project_id):
        calls.append("review")
        return {"cuts": 1, "verdicts": {"pass": 1}, "reports": [], "skipped": []}

    async def _export(_session, _task):
        calls.append("export")
        return {"media_id": 9}

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return None

        def all(self):
            return []

    class _ExportSession:
        async def get(self, *args):
            return None

        async def execute(self, *args, **kwargs):
            return _Result()

    monkeypatch.setattr(continuity, "maybe_review_project", _review)
    monkeypatch.setattr(generation.export_service, "export_project", _export)

    task = SimpleNamespace(user_id=1, project_id=34, episode_id=None, params={}, id=5)
    result = asyncio.run(generation._handle_export(_ExportSession(), task))

    assert calls == ["review", "export"]
    assert result["continuity"]["cuts"] == 1


class _CutAuditSession:
    """只提供 review_cut 需要的 Episode 读取与 add/flush。"""

    def __init__(self, episode):
        self.episode = episode
        self.added: list = []

    async def get(self, model, pk):  # noqa: ANN001 - 测试替身
        return self.episode if getattr(model, "__name__", "") == "Episode" else None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def test_review_cut_also_writes_audit_run(monkeypatch):
    from app.models.agent_run import AgentRun
    from app.services import generation

    prev_asset = SimpleNamespace(id=10, meta={}, path="a.mp4")
    next_asset = SimpleNamespace(id=11, meta={}, path="b.mp4")

    async def _video_asset(session, shot):
        return prev_asset if shot.id == 1 else next_asset

    async def _extract_frame(path, *, at_end):
        return b"\x89PNG\r\n\x1a\nframe"

    async def _no_keyframe(session, shot):
        return None

    class _Provider:
        def supports(self, capability: str) -> bool:
            return capability == "vision"

        def model_for(self, capability: str) -> str:
            return "deepseek-flash"

        async def generate_vision(self, prompt, *, images, system, json_mode, max_tokens):
            return (
                '{"verdict":"fail","checks":{"same_person":"pass",'
                '"costume_continuity":"fail"},"reasons":["校服从黑变灰"]}'
            )

    async def fake_resolve(*args, **kwargs):
        return SimpleNamespace(id=3, provider="deepseek")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    monkeypatch.setattr(generation, "build_provider_from", lambda credential: _Provider())
    monkeypatch.setattr(continuity, "_video_asset", _video_asset)
    monkeypatch.setattr(continuity, "_extract_frame", _extract_frame)
    monkeypatch.setattr(continuity, "_keyframe_image", _no_keyframe)

    session = _CutAuditSession(SimpleNamespace(id=7, project_id=9))
    report = asyncio.run(
        continuity.review_cut(
            session,
            user_id=1,
            prev_shot=_shot(1, 1, title="A", description="a"),
            shot=_shot(2, 2, title="B", description="b"),
        )
    )

    assert report["verdict"] == "fail"
    assert next_asset.meta["continuity_review"]["verdict"] == "fail"
    run = session.added[-1]
    assert isinstance(run, AgentRun)
    assert run.kind == "continuity_review"
    assert run.status == "fail"
    assert run.project_id == 9
    assert run.target_type == "shot"
    assert run.target_id == 2
    assert run.steps == 1
    assert run.summary["from_shot_id"] == 1
    assert run.summary["checks"]["costume_continuity"] == "fail"
