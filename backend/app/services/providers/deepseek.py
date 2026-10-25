"""DeepSeek：文本 / 推理主力，OpenAI 兼容接口；``deepseek-flash`` 同时支持读图。"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from typing import Any

import httpx

from app.services.providers.base import (
    BaseProvider,
    OpenAICompatMixin,
    ProviderError,
    ProviderSpec,
)


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class DeepSeekProvider(OpenAICompatMixin, BaseProvider):
    spec = ProviderSpec(
        key="deepseek",
        label="DeepSeek",
        capabilities=("text", "vision"),
        default_base_url="https://api.deepseek.com",
        models={
            # 2026-10-25 实测：/models 只列 deepseek-flash 与 deepseek-v4-pro；
            # deepseek-v4-flash 等旧名只是别名，统一收敛到官方名。
            "text": ["deepseek-flash", "deepseek-v4-pro"],
            # v4-pro 实测忽略图片、凭文字编造画面，审查只能用 flash。
            "vision": ["deepseek-flash"],
        },
        docs_url="https://api-docs.deepseek.com",
        description="剧本创作、拆解与结构化输出主力模型，成本低；flash 可读图做关键帧审查。",
        # 推理型模型：开 json_object 会让思考 token 暴涨（实测 2.3k → 12k），
        # 长剧本拆解时容易被截断或被网关断开，因此关掉强制 JSON 模式。
        supports_json_mode=False,
    )

    async def test_connection(self) -> tuple[bool, str]:
        try:
            async with self._client(timeout=20) as client:
                resp = await client.get(f"{self.base_url}/models")
            if resp.status_code < 400:
                return True, "连接成功"
            return False, f"HTTP {resp.status_code}：{resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001 - 连通性测试需兜底
            return False, f"连接异常：{exc}"

    async def generate_vision(
        self,
        prompt: str,
        *,
        images: Sequence[tuple[str, bytes]],
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 900,
        json_mode: bool = True,
    ) -> str:
        if not images:
            raise ProviderError("视觉审查缺少图片")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for label, data in images:
            if not data:
                raise ProviderError(f"视觉审查的图片为空：{label}")
            content.append({"type": "text", "text": f"【{label}】"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode()}"
                    },
                }
            )
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        body: dict[str, Any] = {
            "model": self.model_for("vision", model),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # 不开思考：实测一次简单看图会花 611 个推理 token，容易吃满 max_tokens
            # 后返回空内容；关闭后几十 token 就能给出结论。
            "thinking": {"type": "disabled"},
        }
        # 读图 + 关思考时 json_object 是稳定的；不能用 spec.supports_json_mode
        # （那是给长文本拆解关的）。
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with self._client(timeout=90) as client:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                raise ProviderError(
                    f"视觉模型连接被中断（{type(exc).__name__}），可稍后重试。",
                    transient=True,
                ) from exc

            if resp.status_code >= 400:
                raise ProviderError(
                    f"视觉审查失败（HTTP {resp.status_code}）：{resp.text[:300]}",
                    status_code=resp.status_code,
                )
            payload = resp.json()
            try:
                choice = payload["choices"][0]
                text = choice["message"]["content"]
            except (KeyError, IndexError) as exc:
                raise ProviderError(f"视觉响应解析失败：{str(payload)[:300]}") from exc
            if text and text.strip():
                return text
            last_error = ProviderError(
                "视觉模型返回空内容"
                f"（finish_reason={choice.get('finish_reason')}，max_tokens={max_tokens}）"
            )
            if attempt == 0:
                body["max_tokens"] = min(2048, max_tokens * 2)
                await asyncio.sleep(1)
        raise last_error or ProviderError("视觉审查未获得有效响应")
