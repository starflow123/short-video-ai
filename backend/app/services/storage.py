"""媒体文件落盘与资产持久化。"""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from uuid import uuid4

from app.core.config import settings
from app.core.media_auth import signed_media_path
from app.models.media import MediaAsset
from app.services.http import download_bytes
from app.services.providers.base import ProviderError

_EXT_FALLBACK = {"image": ".png", "video": ".mp4", "audio": ".mp3"}


def media_root() -> Path:
    root = Path(settings.media_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def media_url(rel_path: str) -> str:
    return f"{settings.media_url_prefix}/{rel_path.lstrip('/')}"


def to_public_url(url: str | None) -> str | None:
    """把本地媒体地址转换为公网地址（需配置 PUBLIC_MEDIA_BASE_URL）。

    厂商拉取参考图时不带登录态，因此本地地址附带限时签名；
    已有 http 地址（厂商源站）原样返回。
    """
    if not url or not settings.public_media_base_url:
        return None
    if url.startswith("http"):
        return url
    return f"{settings.public_media_base_url.rstrip('/')}{signed_media_path(url)}"


def _guess_ext(kind: str, mime_type: str | None, source_url: str | None) -> str:
    if mime_type:
        ext = mimetypes.guess_extension(mime_type.split(";")[0].strip())
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    if source_url:
        suffix = Path(source_url.split("?")[0]).suffix
        if 0 < len(suffix) <= 5:
            return suffix
    return _EXT_FALLBACK.get(kind, ".bin")


async def _rewrite_faststart(path: Path) -> bool:
    """把 MP4 的 moov 原子移到文件头部。

    厂商直接下发的 mp4 常把 moov 放在末尾（ftyp → mdat → moov），
    浏览器必须先下载完整文件才拿到索引，表现为「无法拖动进度条 / 快进」。
    这里用 ffmpeg 仅做原子重排（-c copy，不重新编码）；失败则保留原文件。
    """
    tmp = path.with_name(f".{path.stem}.faststart.mp4")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(path), "-c", "copy", "-movflags", "+faststart", str(tmp),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            tmp.unlink(missing_ok=True)
            return False
    except FileNotFoundError:
        tmp.unlink(missing_ok=True)
        return False

    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(path)
    return True


async def persist_media(
    session,
    *,
    user_id: int,
    kind: str,
    provider: str | None = None,
    model: str | None = None,
    project_id: int | None = None,
    shot_id: int | None = None,
    task_id: int | None = None,
    data: bytes | None = None,
    url: str | None = None,
    mime_type: str | None = None,
    meta: dict | None = None,
    note: str | None = None,
    duration_seconds: float | None = None,
    source: str = "generated",
) -> MediaAsset:
    if data is None:
        if not url:
            raise ProviderError("生成结果既无二进制内容也无下载地址")
        data = await download_bytes(url)

    rel_path = f"{user_id}/{uuid4().hex}{_guess_ext(kind, mime_type, url)}"
    dest = media_root() / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    # mp4 重排 moov，保证前端可拖动进度条
    if dest.suffix.lower() == ".mp4":
        await _rewrite_faststart(dest)
    size_bytes = dest.stat().st_size

    asset_meta = dict(meta or {})
    if url and url.startswith("http"):
        # 保留厂商原始地址：既可用于后续图生视频，也便于排查
        asset_meta.setdefault("source_url", url)

    asset = MediaAsset(
        user_id=user_id,
        project_id=project_id,
        shot_id=shot_id,
        kind=kind,
        source=source,
        provider=provider,
        model=model,
        filename=dest.name,
        path=str(dest),
        url=media_url(rel_path),
        mime_type=mime_type or mimetypes.guess_type(dest.name)[0],
        size_bytes=size_bytes,
        task_id=task_id,
        duration_seconds=duration_seconds,
        meta=asset_meta or None,
        note=note,
    )
    session.add(asset)
    await session.flush()
    return asset


async def asset_bytes(asset: MediaAsset) -> bytes:
    """读取素材内容：优先本地文件，缺失时回源下载。"""
    path = Path(asset.path)
    if path.exists():
        return path.read_bytes()
    source = (asset.meta or {}).get("source_url") or to_public_url(asset.url)
    if source:
        return await download_bytes(source)
    raise ProviderError(f"素材文件缺失：{asset.filename}")
