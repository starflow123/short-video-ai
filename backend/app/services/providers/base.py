"""厂商适配器统一抽象。

设计目标：业务层只依赖 ``BaseProvider`` 的三类能力（文本 / 图片 / 视频）与
``MediaResult``，无需关心各家协议差异。新增厂商只需实现一个适配器并注册。
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.net import UnsafeUrlError, validate_outbound_url

ProgressCb = Callable[[int], None]


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    capabilities: tuple[str, ...]  # 取值集合：text / image / video / vision（读图审查）
    default_base_url: str
    models: dict[str, list[str]]  # capability -> 可选模型列表（首个为默认）
    docs_url: str = ""
    description: str = ""
    # 是否支持 response_format={"type":"json_object"}。
    # 推理型模型（DeepSeek V4）开启后会大幅拉长思考过程（实测思考 token 从 2.3k 涨到 12k），
    # 既容易被 max_tokens 截断，也会让非流式请求久到被网关断开；
    # 所以这类厂商声明为 False，改由提示词约束 + extract_json 容错解析。
    supports_json_mode: bool = True

    @property
    def default_models(self) -> dict[str, str]:
        return {cap: ids[0] for cap, ids in self.models.items() if ids}


@dataclass
class MediaResult:
    """图片 / 视频生成结果：二选一返回远程 URL 或原始字节。"""

    url: str | None = None
    data: bytes | None = None
    mime_type: str = "application/octet-stream"
    meta: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    """厂商调用异常，消息直接展示在任务中心。

    ``status_code`` / ``transient`` 为可选的附加信息，供兜底链判断是否值得切换候选厂商：
    - ``status_code``：触发异常的 HTTP 状态码（若有）；
    - ``transient``：显式标记该异常是否属于「临时故障」（覆盖默认判定）。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


# 典型的「可重试」HTTP 状态码：请求超时 / 限流 / 服务端瞬时不可用
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_transient_error(exc: BaseException) -> bool:
    """判断异常是否属于临时故障（值得切换候选厂商重试）。

    满足以下任一条件即视为临时故障：
    a) ``ProviderError`` 且显式标记 ``transient=True``；
    b) ``ProviderError`` 且 ``status_code`` 属于可重试状态码
       （408 / 425 / 429 / 500 / 502 / 503 / 504）；
    c) ``httpx.TimeoutException`` 或 ``httpx.TransportError``（网络层瞬时故障）。

    其余情况（参数 / 鉴权类错误）返回 False，避免无意义的重试。
    """
    if isinstance(exc, ProviderError):
        if exc.transient is True:
            return True
        return exc.status_code in _TRANSIENT_STATUS_CODES
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return False


class BaseProvider(ABC):
    spec: ProviderSpec
    # 能力显式声明：是否支持「图像参考」（把角色 / 场景资产图作为参考图一起送进模型）。
    # 未声明的厂商按不支持处理，上层不会传参考图，避免「传了参数却被静默忽略」的隐性故障。
    supports_image_reference: bool = False

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        if not api_key:
            raise ProviderError("未配置 API Key")
        self.api_key = api_key
        self.base_url = (base_url or self.spec.default_base_url).rstrip("/")
        try:
            validate_outbound_url(self.base_url)
        except UnsafeUrlError as exc:
            raise ProviderError(f"厂商地址不安全：{exc}") from exc

    # ---------------- 通用工具 ----------------
    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def model_for(self, capability: str, model: str | None = None) -> str:
        chosen = model or self.spec.default_models.get(capability)
        if not chosen:
            raise ProviderError(f"{self.spec.label} 未提供 {capability} 模型")
        return chosen

    def supports(self, capability: str) -> bool:
        return capability in self.spec.capabilities

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout or settings.generation_timeout,
            headers={**self.auth_headers, "Content-Type": "application/json"},
        )

    async def _poll(
        self,
        fetch: Callable[[], Awaitable[dict[str, Any]]],
        *,
        done: tuple[str, ...],
        failed: tuple[str, ...],
        interval: float = 2.0,
        timeout: float | None = None,
        on_progress: ProgressCb | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or settings.generation_timeout)
        while True:
            payload = await fetch()
            status = str(payload.get("status", "")).lower()
            if on_progress:
                try:
                    on_progress(int(payload.get("progress") or 0))
                except (TypeError, ValueError):
                    pass
            if status in done:
                return payload
            if status in failed:
                raise ProviderError(f"生成失败：{payload.get('error') or status}")
            if time.monotonic() > deadline:
                raise ProviderError("生成任务超时")
            await asyncio.sleep(interval)

    # ---------------- 能力接口（默认不支持，子类按需覆写） ----------------
    async def test_connection(self) -> tuple[bool, str]:
        return True, "该厂商暂不支持连通性测试"

    async def generate_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        raise ProviderError(f"{self.spec.label} 不支持文本生成")

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
        """多图理解：``images`` 为 (图注, 原始字节)，返回模型文本（通常为 JSON）。"""
        raise ProviderError(f"{self.spec.label} 不支持视觉理解")

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str | None = None,
        n: int = 1,
        aspect_ratio: str | None = None,
        reference_image_urls: Sequence[str] | None = None,
        on_progress: ProgressCb | None = None,
    ) -> MediaResult:
        raise ProviderError(f"{self.spec.label} 不支持图片生成")

    async def generate_video(
        self,
        prompt: str,
        *,
        model: str | None = None,
        image_url: str | None = None,
        image_data: bytes | None = None,
        image_filename: str | None = None,
        last_frame_url: str | None = None,
        seed: int | None = None,
        duration: int = 5,
        aspect_ratio: str = "9:16",
        resolution: str | None = None,
        on_progress: ProgressCb | None = None,
    ) -> MediaResult:
        """图生视频。

        ``image_url`` 需公网可访问；``image_data`` 用于支持自行上传的厂商
        （如 Magic Hour），本地自托管场景下无需暴露内网地址。
        ``last_frame_url`` 为可选尾帧（Agnes keyframe 模式支持），用于让本段视频
        结束在下一镜的首帧上，衔接处不会跳变。
        """
        raise ProviderError(f"{self.spec.label} 不支持视频生成")


class OpenAICompatMixin:
    """OpenAI 兼容的 /chat/completions 文本生成实现。

    需与 ``BaseProvider`` 组合使用，且 mixin 放在 MRO 前位：
    ``class XxxProvider(OpenAICompatMixin, BaseProvider)``。
    本 mixin 只覆写 ``generate_text``，``model_for`` / ``_client`` / ``test_connection``
    均由 ``BaseProvider`` 提供，切勿在此重复声明占位实现，否则会因 MRO 顺序遮蔽基类。
    """

    base_url: str
    spec: ProviderSpec

    def _build_messages(self, prompt: str, system: str | None) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    async def generate_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model_for("text", model),
            "messages": self._build_messages(prompt, system),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 只有厂商声明支持时才带 response_format：推理型模型带上它会显著拉长思考，
        # 导致长 JSON 输出被截断、非流式请求被网关断开。
        if json_mode and getattr(self.spec, "supports_json_mode", True):
            body["response_format"] = {"type": "json_object"}

        # 厂商偶发返回空 content（多见于长 JSON 输出被截断），重试一次并放宽 max_tokens；
        # 另外上游网关会在长耗时请求上直接断开（httpx.RemoteProtocolError），
        # 这种属于瞬时故障，同样重试，不能直接抛给用户一句英文协议错误。
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self._client() as client:
                    resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise ProviderError(
                    "文本模型连接被中断（上游网关断开或响应超时）。"
                    f"可稍后重试，或减少一次拆解的分镜数量。原始错误：{type(exc).__name__}",
                    transient=True,
                ) from exc

            if resp.status_code >= 400:
                raise ProviderError(
                    f"文本生成失败（HTTP {resp.status_code}）：{resp.text[:300]}",
                    status_code=resp.status_code,
                )
            payload = resp.json()
            try:
                choice = payload["choices"][0]
                content = choice["message"]["content"]
            except (KeyError, IndexError) as exc:
                raise ProviderError(f"文本响应解析失败：{payload}") from exc
            if content and content.strip():
                return content
            # 空内容：多半是思考 token 吃满预算被截断，放宽一次上限再试
            if attempt < 2:
                body["max_tokens"] = min(32768, int(body.get("max_tokens") or 4096) * 2)
                await asyncio.sleep(1)
                continue
            raise ProviderError(
                "文本模型返回空内容"
                f"（finish_reason={choice.get('finish_reason')}，max_tokens={body.get('max_tokens')}）。"
                "通常是模型思考过长把输出预算用尽，可减少分镜数量或换用非推理模型。"
            )
        raise ProviderError(f"文本生成失败：{last_error or '未获得有效响应'}")
