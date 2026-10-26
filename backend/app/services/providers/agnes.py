"""Agnes AI：文本 / 图片 / 视频三模态，核心模型当前免费。

接口要点（依据官方文档 2026-10，中国站）：
- Base：``https://api.agnes-ai.cn/v1``（国际站 apihub.agnes-ai.com 的 Key 不通用）
- 文本：``POST /chat/completions``（OpenAI 兼容）
- 图片：``POST /images/generations``，body ``{model, prompt, size, ratio, image[]}``；
  ``size`` 为档位 ``1K|2K|3K|4K``，``ratio`` 为构图比例（注意不是 ``aspect_ratio``）。
- 视频：``POST /videos`` 创建异步任务；轮询 ``GET {root}/agnesapi?video_id=..&model_name=..``
  完成后取顶层 ``url`` 字段。免费档会返回 429 / 503，需友好提示。
"""
from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Sequence
from typing import Any

from app.services.providers.base import (
    BaseProvider,
    MediaResult,
    OpenAICompatMixin,
    ProgressCb,
    ProviderError,
    ProviderSpec,
)

_VIDEO_DONE = ("completed", "complete", "succeeded", "success")
_VIDEO_FAILED = ("failed", "error", "canceled", "cancelled")

_IMAGE_SIZE_TIERS = ("1K", "2K", "3K", "4K")
_IMAGE_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")
_PIXEL_SIZE_RE = re.compile(r"^(\d+)\s*[xX×]\s*(\d+)$")

# Agnes 视频接口只认这一档，且必须是大写：实测 size=1080p 返回 400 invalid_request
# （valid_sizes=["720P"]），size=720p 小写返回 402 plan_upgrade_required，大写 720P 才建单成功。
_VIDEO_SIZES = ("720P",)


def _video_size(value: str | None) -> str:
    """把 720p / 1080P / 1920x1080 这类写法收敛成 Agnes 认的档位枚举。"""
    normalized = str(value or "").strip().upper()
    if normalized in _VIDEO_SIZES:
        return normalized
    return _VIDEO_SIZES[0]


def _image_size(value: str | None) -> str:
    """归一化尺寸：支持档位 ``1K..4K``，也兼容 ``1024x1024`` 这类像素写法。"""
    if not value:
        return "1K"
    normalized = str(value).strip().upper()
    if normalized in _IMAGE_SIZE_TIERS:
        return normalized
    match = _PIXEL_SIZE_RE.match(normalized)
    if match:
        long_edge = max(int(match.group(1)), int(match.group(2)))
        if long_edge <= 1280:
            return "1K"
        if long_edge <= 2048:
            return "2K"
        if long_edge <= 3072:
            return "3K"
        return "4K"
    return "1K"


def _image_ratio(value: str | None) -> str | None:
    if value and str(value).strip() in _IMAGE_RATIOS:
        return str(value).strip()
    return None


def _friendly_http_error(action: str, status_code: int, body: str) -> str:
    if status_code == 429:
        return f"{action}失败：免费额度触发限流（429 rate_limit_exceeded），请稍后重试。{body[:200]}"
    if status_code == 503:
        return f"{action}失败：队列已满（503 video_queue_full），请稍后重试。{body[:200]}"
    if status_code in (401, 403):
        return f"{action}失败：密钥无效或无权限（HTTP {status_code}），请到「密钥管理」检查 Key 与 Base URL。{body[:200]}"
    if status_code == 402:
        return (
            f"{action}失败：当前 Agnes 账户的套餐不支持这一档（402 plan_upgrade_required），"
            f"需要在 Agnes 后台升级套餐，或在「密钥管理」换一把有该档权限的 Key。{body[:200]}"
        )
    return f"{action}失败（HTTP {status_code}）：{body[:300]}"


class AgnesProvider(OpenAICompatMixin, BaseProvider):
    spec = ProviderSpec(
        key="agnes",
        label="Agnes AI",
        capabilities=("text", "image", "video"),
        default_base_url="https://api.agnes-ai.cn/v1",
        models={
            "text": ["agnes-2.5-flash", "agnes-2.5-pro", "agnes-3.0-flash", "agnes-3.0-pro"],
            "image": ["agnes-image-2.5-flash", "agnes-image-2.1-flash"],
            "video": ["agnes-video-2.5-flash", "agnes-video-2.5"],
        },
        docs_url="https://wiki.agnes-ai.com",
        description="中国站（api.agnes-ai.cn）文本 / 图片 / 视频三模态，核心模型免费。",
    )

    # 支持「图像参考」（图生图）：把角色 / 场景资产的参考图一起送给模型，
    # 这是让同一角色在不同镜头里长得一样的关键手段。
    supports_image_reference = True

    # ---------------- 公共 ----------------
    @property
    def root_url(self) -> str:
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    async def test_connection(self) -> tuple[bool, str]:
        try:
            async with self._client(timeout=20) as client:
                resp = await client.get(f"{self.base_url}/models")
            if resp.status_code < 400:
                return True, "连接成功"
            return False, f"HTTP {resp.status_code}：{resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            return False, f"连接异常：{exc}"

    # ---------------- 图片 ----------------
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
        model_id = self.model_for("image", model)
        body: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "size": _image_size(size),
        }
        ratio = _image_ratio(aspect_ratio)
        if ratio:
            body["ratio"] = ratio
        # 多图参考：数组顺序与提示词里的「图1/图2…」图例一一对应
        references = [str(url).strip() for url in (reference_image_urls or []) if str(url).strip()]
        # 去重但保持顺序
        references = list(dict.fromkeys(references))[:4]
        if references:
            body["image"] = references

        # 带参考图的请求走「图生图队列」，免费档高峰期会整段返回 503「队列已满」，
        # 只能排队重试。不换成另一个模型：两个模型的笔触与上色习惯不同，
        # 同一批关键帧混用会直接看出画风漂移（实测 2.5 与 2.1 混出的帧并排不协调）。
        # 也不摘掉参考图降级成纯文生图：那样这一帧没有长相锚点，必然画成另一个人，
        # 还会被继续拿去做视频，代价比失败一张图大得多。排队到点就显式报错让人重跑。
        attempts = 14 if references else 1
        reference_used = bool(references)
        used_model = model_id
        resp = None
        for attempt in range(attempts):
            async with self._client() as client:
                resp = await client.post(f"{self.base_url}/images/generations", json=body)
            if resp.status_code < 400:
                break
            if not (reference_used and resp.status_code in (429, 503)):
                raise ProviderError(
                    _friendly_http_error("图片生成", resp.status_code, resp.text),
                    status_code=resp.status_code,
                )
            if attempt < attempts - 1:
                await asyncio.sleep(min(30, 4 * (attempt + 1)))
                continue
            raise ProviderError(
                f"图片生成失败：Agnes 图生图队列已满（{resp.status_code}），"
                f"已在 {model_id} 上排队重试 {attempts} 次仍未进队。"
                "等 1-2 分钟后重跑这一张即可；系统不会换模型或摘参考图降级出图，"
                "那样画风会漂、人物一定不像本人。",
                status_code=resp.status_code,
            )

        if resp.status_code >= 400:
            raise ProviderError(
                _friendly_http_error("图片生成", resp.status_code, resp.text),
                status_code=resp.status_code,
            )

        payload = resp.json()
        items = payload.get("data") or []
        if not items:
            raise ProviderError(f"图片响应缺少 data 字段：{payload}")
        item = items[0]
        if on_progress:
            on_progress(100)
        meta = {
            "model": used_model,
            "size": body["size"],
            "ratio": ratio,
            # 本次是否真的用上了参考图：为 False 说明参考图队列满、已降级为纯文生图
            "reference_used": reference_used,
            "reference_count": len(references),
        }
        if item.get("url"):
            return MediaResult(url=item["url"], mime_type="image/png", meta=meta)
        if item.get("b64_json"):
            return MediaResult(data=base64.b64decode(item["b64_json"]), mime_type="image/png", meta=meta)
        raise ProviderError(f"图片响应无法解析：{item}")

    # ---------------- 视频 ----------------
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
        if image_data and not image_url:
            raise ProviderError(
                "Agnes 图生视频要求首帧为公网可访问的图片地址（如 Agnes 自己生成的图片 URL）；"
                "本地素材请配置 PUBLIC_MEDIA_BASE_URL 后再试。"
            )
        model_id = self.model_for("video", model)
        seconds = str(max(4, min(12, int(duration or 5))))
        mode = "keyframe" if (image_url or last_frame_url) else "text"

        body: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "seconds": seconds,
            "mode": mode,
            "size": _video_size(resolution),
            "aspect_ratio": aspect_ratio,
        }
        if image_url:
            body["first_frame"] = image_url
        if last_frame_url:
            body["last_frame"] = last_frame_url
        if seed is not None:
            body["seed"] = int(seed)

        async with self._client() as client:
            # 免费档「创建」接口经常返回 429 / 503，做几次退避重试以提高成功率。
            create = None
            for attempt in range(4):
                create = await client.post(f"{self.base_url}/videos", json=body)
                if create.status_code < 400:
                    break
                if create.status_code in (429, 503) and attempt < 3:
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                raise ProviderError(
                    _friendly_http_error("视频任务创建", create.status_code, create.text),
                    status_code=create.status_code,
                )
            if create is None or create.status_code >= 400:
                status_code = create.status_code if create else 503
                text = create.text if create else "队列持续繁忙"
                raise ProviderError(
                    _friendly_http_error("视频任务创建", status_code, text),
                    status_code=status_code,
                )

            created = create.json()
            video_id = created.get("video_id") or created.get("id")
            if not video_id:
                raise ProviderError(f"视频任务响应缺少 video_id：{created}")

            async def fetch() -> dict[str, Any]:
                resp = await client.get(
                    f"{self.root_url}/agnesapi",
                    params={"video_id": video_id, "model_name": model_id},
                )
                # 免费档对「创建」与「查询」都有严格限流，把 429 / 503 视为暂时不可用，
                # 返回 pending 让轮询继续等待，而不是直接判失败。
                if resp.status_code in (429, 503):
                    return {"status": "pending", "progress": 0}
                if resp.status_code >= 400:
                    raise ProviderError(
                        _friendly_http_error("视频状态查询", resp.status_code, resp.text),
                        status_code=resp.status_code,
                    )
                return resp.json()

            final = await self._poll(
                fetch,
                done=_VIDEO_DONE,
                failed=_VIDEO_FAILED,
                interval=5.0,
                on_progress=on_progress,
            )

        if not final.get("url"):
            raise ProviderError(f"视频完成但缺少播放地址：{final}")
        used_size = _video_size(resolution)
        meta: dict[str, Any] = {"model": model_id, "duration": int(seconds), "mode": mode}
        if resolution and str(resolution).strip().upper() != used_size:
            # 降档必须让用户看得见：分镜里 AI 选的 1080p 到 Agnes 只会出 720P，不能静默当交付了高清
            meta["resolution_note"] = (
                f"Agnes 视频只有 {used_size} 一档，请求的 {resolution} 已按 {used_size} 出片。"
            )
        meta["resolution"] = used_size
        return MediaResult(url=final["url"], mime_type="video/mp4", meta=meta)
