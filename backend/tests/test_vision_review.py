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
    )

    assert review["checks"]["face_match"] == "pass"
    assert review["checks"]["costume_match"] == "pass"
    assert review["checks"]["duplicate_person"] == "pass"
    assert review["verdict"] == "pass"
