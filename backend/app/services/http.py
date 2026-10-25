"""通用 HTTP 工具：下载远程素材 / 上传到厂商存储。

注意：下载第三方 URL 时使用「不带鉴权头」的独立客户端，
避免把用户的 API Key 泄露给外部主机。
"""
from __future__ import annotations

import asyncio

import httpx

from app.core.net import UnsafeUrlError, validate_outbound_url

_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_MAX_REDIRECTS = 5


def _error(message: str) -> Exception:
    # 延迟导入：避免 storage/http ↔ providers 之间的循环导入
    from app.services.providers.base import ProviderError

    return ProviderError(message)


async def _check_url(url: str) -> None:
    try:
        await asyncio.to_thread(validate_outbound_url, url)
    except UnsafeUrlError as exc:
        raise _error(f"素材地址不安全：{exc}") from exc


async def download_bytes(url: str, timeout: float = 300.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            await _check_url(current)
            async with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise _error(f"素材下载被重定向但缺少目标地址（HTTP {resp.status_code}）")
                    current = str(httpx.URL(current).join(location))
                    continue
                if resp.status_code >= 400:
                    raise _error(f"素材下载失败（HTTP {resp.status_code}）")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > _MAX_DOWNLOAD_BYTES:
                    raise _error("素材过大，已拒绝下载")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_DOWNLOAD_BYTES:
                        raise _error("素材过大，已拒绝下载")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise _error("素材下载重定向次数过多")


async def put_bytes(url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.put(url, content=data, headers={"Content-Type": content_type})
    if resp.status_code >= 400:
        raise _error(f"素材上传失败（HTTP {resp.status_code}）：{resp.text[:200]}")
