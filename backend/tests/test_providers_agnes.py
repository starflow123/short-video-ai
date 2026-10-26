"""Agnes 图片请求体：多参考图按顺序发送，去重且可观测；队列满时不静默降级。"""
from __future__ import annotations

import asyncio

import pytest

from app.services.providers.agnes import AgnesProvider
from app.services.providers.base import ProviderError


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"data": [{"url": "https://cdn.example.com/out.png"}]}


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


def test_agnes_sends_multiple_reference_images_in_order():
    provider = AgnesProvider("test-key")
    client = _FakeClient()
    provider._client = lambda timeout=None: client  # type: ignore[method-assign]

    result = asyncio.run(
        provider.generate_image(
            "杨间撑开黑色鬼伞",
            reference_image_urls=[
                "https://cdn.example.com/yang.png",
                "https://cdn.example.com/umbrella.png",
                "https://cdn.example.com/yang.png",
            ],
        )
    )

    assert client.bodies[0]["image"] == [
        "https://cdn.example.com/yang.png",
        "https://cdn.example.com/umbrella.png",
    ]
    assert result.meta["reference_used"] is True
    assert result.meta["reference_count"] == 2


class _QueueFullResponse:
    status_code = 503
    text = '{"error":"video_queue_full"}'

    def json(self):
        return {"error": "video_queue_full"}


class _AlwaysBusyClient:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json):  # noqa: A002 - 对齐 httpx 参数名
        self.bodies.append(json)
        return _QueueFullResponse()


def test_queue_full_fails_loudly_instead_of_dropping_references(monkeypatch):
    """实测：Agnes 图生图队列经常整段 503。摘掉参考图降级出图会画成另一个人，
    还会被继续拿去做视频，所以必须报错让人重跑，而不是静默出脏帧。"""
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.services.providers.agnes.asyncio.sleep", _no_sleep)

    provider = AgnesProvider("test-key")
    client = _AlwaysBusyClient()
    provider._client = lambda timeout=None: client  # type: ignore[method-assign]

    with pytest.raises(ProviderError) as exc:
        asyncio.run(
            provider.generate_image(
                "杨间撑开黑色鬼伞",
                reference_image_urls=["https://cdn.example.com/yang.png"],
            )
        )

    assert "队列已满" in str(exc.value)
    # 每一次重试都带着参考图：没有任何一次请求被摘掉 image 降级成纯文生图
    assert all(body.get("image") for body in client.bodies)
    # 每一次重试都在同一个模型上排队：换模型会让同一批关键帧混出两种画风
    assert len({body["model"] for body in client.bodies}) == 1
    assert len(client.bodies) > 1


class _VideoFakeClient:
    """记录 /videos 建单请求体，查询立即返回完成，用于断言 size 字段。"""

    def __init__(self) -> None:
        self.body: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json):  # noqa: A002 - 对齐 httpx 参数名
        self.body = json

        class _Created:
            status_code = 200
            text = ""

            def json(self):
                return {"video_id": "vid-1"}

        return _Created()

    async def get(self, url, params=None):
        class _Done:
            status_code = 200
            text = ""

            def json(self):
                return {"status": "completed", "url": "https://cdn.example.com/out.mp4"}

        return _Done()


@pytest.mark.parametrize(
    "requested,expected_size,expect_note",
    [
        ("1080p", "720P", True),   # 分镜里 AI 选的 1080p：实测直接送过去返回 400
        ("720p", "720P", False),   # 同一档的大小写写法，不算降档
        ("720P", "720P", False),   # 只有完全匹配的这一档能建单
        (None, "720P", False),
    ],
)
def test_agnes_video_size_is_normalized_to_the_only_supported_tier(requested, expected_size, expect_note):
    provider = AgnesProvider("test-key")
    client = _VideoFakeClient()
    provider._client = lambda timeout=None: client  # type: ignore[method-assign]

    result = asyncio.run(
        provider.generate_video(
            "少女低头抚猫，海风轻拂",
            image_url="https://cdn.example.com/frame.png",
            resolution=requested,
        )
    )

    assert client.body["size"] == expected_size
    assert result.meta["resolution"] == expected_size
    assert ("resolution_note" in result.meta) is expect_note
    if expect_note:
        assert requested in result.meta["resolution_note"]
