"""DeepSeek：文本 / 推理主力，OpenAI 兼容接口。"""
from __future__ import annotations

from app.services.providers.base import BaseProvider, OpenAICompatMixin, ProviderSpec


class DeepSeekProvider(OpenAICompatMixin, BaseProvider):
    spec = ProviderSpec(
        key="deepseek",
        label="DeepSeek",
        capabilities=("text",),
        default_base_url="https://api.deepseek.com",
        models={
            "text": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"],
        },
        docs_url="https://api-docs.deepseek.com",
        description="剧本创作、拆解与结构化输出主力模型，成本低。",
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
