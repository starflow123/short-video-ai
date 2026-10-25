"""Agnes 图片请求体：多参考图按顺序发送，去重且可观测。"""
from __future__ import annotations

import asyncio

from app.services.providers.agnes import AgnesProvider


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
