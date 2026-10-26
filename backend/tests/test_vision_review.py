"""关键帧视觉初审：请求体、结论归一化与审查提示词。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.agents import visual_qa
from app.services.providers.deepseek import DeepSeekProvider


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "choices": [
                {"message": {"content": '{"verdict":"pass","checks":{},"reasons":[]}'}}
            ]
        }


class _FakeClient:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json):  # noqa: A002 - 对齐 httpx 参数名
        self.bodies.append(json)
        return _FakeResponse()


def test_deepseek_spec_exposes_vision_and_flash_model():
    assert "vision" in DeepSeekProvider.spec.capabilities
    assert DeepSeekProvider.spec.default_models["vision"] == "deepseek-flash"


def test_deepseek_vision_disables_thinking_and_sends_base64_image():
    provider = DeepSeekProvider("test-key")
    client = _FakeClient()
    provider._client = lambda timeout=None: client  # type: ignore[method-assign]

    text = asyncio.run(
        provider.generate_vision(
            "审查这张图",
            images=[("待审关键帧", b"\x89PNG\r\n\x1a\nfake")],
            system="只输出 JSON",
        )
    )

    assert text.startswith('{"verdict"')
    body = client.bodies[0]
    assert body["model"] == "deepseek-flash"
    # 思考会吃满 max_tokens 并返回空内容，必须显式关闭
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    content = body["messages"][-1]["content"]
    image_parts = [part for part in content if part.get("type") == "image_url"]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_any_failed_check_overrides_model_pass():
    review = visual_qa.normalize_review(
        {
            "verdict": "pass",
            "checks": {
                "face_match": "fail",
                "costume_match": "pass",
                "cast_count": "pass",
                "prop_holder": "pass",
                "duplicate_person": "pass",
                "composition": "pass",
            },
            "reasons": ["陈默的脸比定妆照更圆"],
            "prompt_patch": "陈默脸型更瘦，下颌线清晰",
        },
        provider="deepseek",
        model="deepseek-flash",
        media_id=101,
    )

    assert review["verdict"] == "fail"
    assert review["checks"]["face_match"] == "fail"
    assert review["advisory"] is True
    assert review["prompt_patch"] == "陈默脸型更瘦，下颌线清晰"
    assert review["media_id"] == 101


def test_unknown_check_is_warn_and_bad_values_normalize():
    review = visual_qa.normalize_review(
        {
            "verdict": "whatever",
            "checks": {"face_match": "看不清", "cast_count": 2, "composition": "pass"},
            "reasons": [f"原因{i}" for i in range(8)],
        },
        provider="deepseek",
        model="deepseek-flash",
        media_id=1,
    )

    assert review["checks"]["face_match"] == "unknown"
    assert review["checks"]["cast_count"] == "unknown"
    assert review["checks"]["composition"] == "pass"
    assert review["verdict"] == "warn"
    assert len(review["reasons"]) == 5


def test_review_prompt_states_cast_count_and_prop_owner():
    shot = SimpleNamespace(
        title="纸条与警告",
        description="林越把纸条推给陈默，陈默举着保温杯",
        image_prompt="教室午后，两名男生对坐",
        camera=None,
        directives={"shot_size": "中景", "composition": "桌面在下方"},
        subjects=[
            {"name": "林越", "appearance": "脸型瘦削", "costume": "深蓝校服", "action": "推纸条"},
            {"name": "陈默", "appearance": "脸型方圆", "costume": "浅灰卫衣", "holds": "保温杯"},
        ],
        props=[{"name": "银色保温杯", "owner": "陈默", "state": "握在手里"}],
    )

    prompt = visual_qa.build_review_prompt(
        shot, image_labels=["图1：待审关键帧", "图2：角色「林越」的定妆照"]
    )

    assert "画面应恰好出现 2 个可辨认人物" in prompt
    assert "深蓝校服" in prompt
    assert "应由 陈默 持有" in prompt
    assert "看不清给 unknown" in prompt
    assert "图2：角色「林越」的定妆照" in prompt


def test_auto_review_switch_off_makes_no_call(monkeypatch):
    monkeypatch.setattr(visual_qa.settings, "vision_review_enabled", False)
    result = asyncio.run(
        visual_qa.maybe_review_keyframe(None, user_id=1, asset=None, shot=None)
    )
    assert result is None


def test_auto_review_swallows_provider_failures(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("vision down")

    monkeypatch.setattr(visual_qa.settings, "vision_review_enabled", True)
    monkeypatch.setattr(visual_qa, "review_keyframe", _boom)
    result = asyncio.run(
        visual_qa.maybe_review_keyframe(
            None, user_id=1, asset=SimpleNamespace(id=1), shot=SimpleNamespace()
        )
    )
    assert result is None


def test_empty_shot_treats_person_checks_as_not_applicable():
    review = visual_qa.normalize_review(
        {
            "verdict": "warn",
            "checks": {
                "face_match": "unknown",
                "costume_match": "unknown",
                "cast_count": "pass",
                "prop_holder": "pass",
                "duplicate_person": "unknown",
                "composition": "pass",
            },
            "reasons": [],
        },
        provider="deepseek",
        model="deepseek-flash",
        media_id=9,
        subject_count=0,
        has_style=False,
    )

    assert review["checks"]["face_match"] == "pass"
    assert review["checks"]["costume_match"] == "pass"
    assert review["checks"]["duplicate_person"] == "pass"
    # 项目没填视觉风格：画风一项不适用，不能算成风险
    assert review["checks"]["style_match"] == "pass"
    assert review["verdict"] == "pass"


def test_photoreal_frame_fails_hand_drawn_style():
    """实测缺口：要求「手绘动画」却出成真人照片，之前没有任何检查项会报出来。"""
    prompt = visual_qa.build_review_prompt(
        SimpleNamespace(
            title="站牌下的成绩单",
            description="小满坐在木长椅上低头看成绩单",
            image_prompt="小满低头看成绩单",
            camera=None,
            directives={"shot_size": "中景"},
            subjects=[{"name": "小满", "costume": "米色针织开衫"}],
            props=[],
        ),
        image_labels=["图1：待审关键帧"],
        visual_style="日系手绘动画质感，柔光逆光",
    )
    assert "日系手绘动画质感，柔光逆光" in prompt
    assert "style_match" in prompt

    review = visual_qa.normalize_review(
        {
            "verdict": "pass",
            "checks": {
                "face_match": "pass",
                "costume_match": "pass",
                "cast_count": "pass",
                "prop_holder": "pass",
                "duplicate_person": "pass",
                "composition": "pass",
                "style_match": "fail",
            },
            "reasons": ["要求手绘动画，画面是真人照片"],
        },
        provider="deepseek",
        model="deepseek-flash",
        media_id=9,
        subject_count=1,
        has_style=True,
    )

    assert review["checks"]["style_match"] == "fail"
    assert review["verdict"] == "fail"


def test_review_without_style_omits_style_rule():
    prompt = visual_qa.build_review_prompt(
        SimpleNamespace(
            title="空镜",
            description="夜色里的站牌",
            image_prompt="空镜",
            camera=None,
            directives={},
            subjects=[],
            props=[],
        ),
        image_labels=["图1：待审关键帧"],
        visual_style=None,
    )

    assert "style_match" not in prompt


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def scalars(self):
        return _Scalars([])


class _AuditSession:
    """只满足 review_keyframe 的取数与写入，用来断言审计落库。"""

    def __init__(self, episode):
        self.episode = episode
        self.added: list = []

    async def get(self, model, pk):  # noqa: ANN001 - 测试替身
        return self.episode if getattr(model, "__name__", "") == "Episode" else None

    async def execute(self, _stmt):
        return _Result()

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def test_review_keyframe_also_writes_audit_run(monkeypatch):
    from app.models.agent_run import AgentRun
    from app.services import generation, storage

    class _Provider:
        def supports(self, capability: str) -> bool:
            return capability == "vision"

        def model_for(self, capability: str) -> str:
            return "deepseek-flash"

        async def generate_vision(self, prompt, *, images, system, json_mode, max_tokens):
            return (
                '{"verdict":"pass","checks":{"face_match":"pass","cast_count":"pass",'
                '"prop_holder":"pass","composition":"pass"},"reasons":[]}'
            )

    async def fake_resolve(*args, **kwargs):
        return SimpleNamespace(id=3, provider="deepseek")

    monkeypatch.setattr(generation, "resolve_credential", fake_resolve)
    monkeypatch.setattr(generation, "build_provider_from", lambda credential: _Provider())

    async def fake_bytes(media):
        return b"\x89PNG\r\n\x1a\nframe"

    monkeypatch.setattr(storage, "asset_bytes", fake_bytes)

    shot = SimpleNamespace(
        id=5,
        episode_id=7,
        title="午后教室",
        description="两名男生对坐",
        image_prompt="午后教室",
        camera=None,
        directives={},
        subjects=[],
        props=[],
    )
    asset = SimpleNamespace(id=11, meta={})
    session = _AuditSession(SimpleNamespace(id=7, project_id=9))

    review = asyncio.run(
        visual_qa.review_keyframe(session, user_id=1, asset=asset, shot=shot)
    )

    assert review["verdict"] == "pass"
    run = session.added[-1]
    assert isinstance(run, AgentRun)
    assert run.kind == "visual_review"
    assert run.status == "pass"
    assert run.project_id == 9
    assert run.target_type == "media"
    assert run.target_id == 11
    assert run.steps == 1
    assert run.summary["shot_id"] == 5
    assert run.summary["checks"]["face_match"] == "pass"
