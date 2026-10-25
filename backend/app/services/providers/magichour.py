"""Magic Hour：多模型聚合的图片 / 视频生成 API。

接口要点（依据官方文档 2026-10）：
- 创建任务：``POST /ai-image-generator``、``POST /text-to-video``、``POST /image-to-video``
  返回 ``{"id": ..., "credits_charged": ...}``
- 查询任务：``GET /image-projects/{id}``、``GET /video-projects/{id}``
  完成后从 ``downloads[0].url`` 取结果
- 图片作为首帧输入需先 ``POST /files/upload-urls`` 获取预签名地址，
  ``PUT`` 上传后拿到 ``file_path``
"""
from __future__ import annotations

from typing import Any

from app.services.http import download_bytes, put_bytes
from app.services.providers.base import (
    BaseProvider,
    MediaResult,
    ProgressCb,
    ProviderError,
    ProviderSpec,
)

_DONE = ("complete", "completed", "succeeded", "success")
_FAILED = ("error", "failed", "canceled", "cancelled")


class MagicHourProvider(BaseProvider):
    spec = ProviderSpec(
        key="magichour",
        label="Magic Hour",
        capabilities=("image", "video"),
        default_base_url="https://api.magichour.ai/v1",
        models={
            "image": [
                "flux-schnell",
                "z-image-turbo",
                "krea-2",
                "flux-2-klein",
                "nano-banana-2.1",
                "seedream-v4",
                "gpt-image-2",
                "qwen-image-2.1",
            ],
            "video": [
                "ltx-2.5",
                "minimax-h3",
                "kling-3.0",
                "seedance-2.5",
                "veo3.1",
                "wan-3.0",
                "sora-2",
                "seedance-2.0-mini",
            ],
        },
        docs_url="https://docs.magichour.ai",
        description="多模型聚合；免费档可用 LTX 2.5 / MiniMax H3（480p），失败不扣费。",
    )

    async def test_connection(self) -> tuple[bool, str]:
        try:
            async with self._client(timeout=20) as client:
                resp = await client.get(f"{self.base_url}/account")
            if resp.status_code < 400:
                payload = resp.json()
                return True, f"连接成功，可用额度：{payload.get('credits', '未知')}"
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
        reference_image_urls: list[str] | None = None,
        on_progress: ProgressCb | None = None,
    ) -> MediaResult:
        # Magic Hour 图片接口当前只接受文本提示词，参考图由上层按能力开关拦截，不会传到这里。
        model_id = self.model_for("image", model)
        # 分辨率可选 auto / 640px / 1k / 2k / 4k；免费档不支持 1k 及以上，默认 auto 以兼容各档
        body: dict[str, Any] = {
            "name": "ai-video-studio image",
            "image_count": n,
            "model": model_id,
            "aspect_ratio": aspect_ratio or "1:1",
            "resolution": size or "auto",
            "style": {"prompt": prompt},
        }
        async with self._client() as client:
            created = await self._create(client, "/ai-image-generator", body)
            project_id = created.get("id")
            if not project_id:
                raise ProviderError(f"图片任务响应缺少 id：{created}")

            async def fetch() -> dict[str, Any]:
                resp = await client.get(f"{self.base_url}/image-projects/{project_id}")
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"图片状态查询失败（HTTP {resp.status_code}）：{resp.text[:200]}",
                        status_code=resp.status_code,
                    )
                return resp.json()

            final = await self._poll(fetch, done=_DONE, failed=_FAILED, on_progress=on_progress)

        return self._to_media(final, kind="image", model=model_id)

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
        model_id = self.model_for("video", model)
        res = resolution or "480p"
        body: dict[str, Any] = {
            "name": "ai-video-studio video",
            "model": model_id,
            "end_seconds": max(1, min(60, int(duration or 5))),
            "resolution": res,
            "style": {"prompt": prompt},
        }

        async with self._client() as client:
            if image_data or image_url:
                if image_data:
                    extension = self._extension_of(image_filename)
                    data = image_data
                else:
                    data = await download_bytes(image_url or "")
                    extension = "png" if (image_url or "").lower().endswith(".png") else "jpg"
                file_path = await self._upload_image_bytes(client, data, extension)
                body["assets"] = {"image_file_path": file_path}
                created = await self._create(client, "/image-to-video", body)
            else:
                body["aspect_ratio"] = aspect_ratio
                created = await self._create(client, "/text-to-video", body)

            project_id = created.get("id")
            if not project_id:
                raise ProviderError(f"视频任务响应缺少 id：{created}")

            async def fetch() -> dict[str, Any]:
                resp = await client.get(f"{self.base_url}/video-projects/{project_id}")
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"视频状态查询失败（HTTP {resp.status_code}）：{resp.text[:200]}",
                        status_code=resp.status_code,
                    )
                return resp.json()

            final = await self._poll(fetch, done=_DONE, failed=_FAILED, on_progress=on_progress)

        meta = {"model": model_id, "duration": body["end_seconds"], "resolution": res}
        if image_data or image_url:
            meta["mode"] = "keyframe"
        return self._to_media(final, kind="video", model=model_id, extra_meta=meta)

    # ---------------- 内部工具 ----------------
    async def _create(self, client, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await client.post(f"{self.base_url}{path}", json=body)
        if resp.status_code >= 400:
            raise ProviderError(
                f"任务创建失败（HTTP {resp.status_code}）：{resp.text[:300]}",
                status_code=resp.status_code,
            )
        return resp.json()

    def _extension_of(self, filename: str | None) -> str:
        if filename and filename.lower().endswith(".png"):
            return "png"
        return "jpg"

    async def _upload_image_bytes(self, client, data: bytes, extension: str) -> str:
        """把首帧图片上传到 Magic Hour 存储，返回 file_path。"""
        resp = await client.post(
            f"{self.base_url}/files/upload-urls",
            json={"items": [{"type": "image", "extension": extension}]},
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"申请上传地址失败（HTTP {resp.status_code}）：{resp.text[:200]}",
                status_code=resp.status_code,
            )
        item = (resp.json().get("items") or [{}])[0]
        upload_url = item.get("upload_url")
        file_path = item.get("file_path")
        if not upload_url or not file_path:
            raise ProviderError(f"上传地址响应异常：{item}")
        content_type = "image/png" if extension == "png" else "image/jpeg"
        await put_bytes(upload_url, data, content_type)
        return file_path

    @staticmethod
    def _to_media(
        payload: dict[str, Any], *, kind: str, model: str, extra_meta: dict | None = None
    ) -> MediaResult:
        downloads = payload.get("downloads") or []
        if not downloads:
            raise ProviderError(f"任务完成但无下载地址：{payload}")
        url = downloads[0].get("url") or downloads[0].get("file_url")
        if not url:
            raise ProviderError(f"下载地址解析失败：{downloads[0]}")
        mime = "video/mp4" if kind == "video" else "image/png"
        meta = {"model": model, "credits_charged": payload.get("credits_charged")}
        meta.update(extra_meta or {})
        return MediaResult(url=url, mime_type=mime, meta=meta)
