"""成片导出：用 FFmpeg 把项目内各分集的分镜视频拼接为一个文件。"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select

from app.models.episode import Episode
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.shot import Shot
from app.services import storage
from app.services.providers.base import ProviderError

_TARGET_SIZE = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (1080, 1080)}
# 观众感知的是「切点跳变」而不是整段平均色，因此按每一段的首/尾取样做配平。
_GRADE_STRENGTH = 0.7
_MAX_MEAN_SHIFT = 28.0
_MIN_GAIN, _MAX_GAIN = 0.75, 1.35
_PROBE_W, _PROBE_H = 64, 114


async def _sample_stats(path: Path, points: list[float]) -> tuple[list[float], list[float]] | None:
    means: list[list[float]] = []
    stds: list[list[float]] = []
    for at in points:
        cmd = [
            "ffmpeg", "-v", "error", "-ss", f"{max(at, 0.05):.2f}", "-i", str(path),
            "-frames:v", "1", "-vf", f"scale={_PROBE_W}:{_PROBE_H}", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            raw, _ = await proc.communicate()
        except FileNotFoundError:
            return None
        n = len(raw) // 3
        if n == 0:
            continue
        channel_means = [sum(raw[i::3]) / n for i in range(3)]
        means.append(channel_means)
        stds.append([
            (sum((v - channel_means[i]) ** 2 for v in raw[i::3]) / n) ** 0.5 for i in range(3)
        ])
    if not means:
        return None
    count = len(means)
    return (
        [sum(m[c] for m in means) / count for c in range(3)],
        [sum(s[c] for s in stds) / count for c in range(3)],
    )


async def _probe_cut_stats(path: Path, duration: float | None) -> tuple[tuple, tuple] | None:
    """取一段视频开头与结尾的颜色统计，用于和相邻段对齐。"""
    total = duration if duration and duration > 0 else 4.0
    head = await _sample_stats(path, [total * 0.04, total * 0.12])
    tail = await _sample_stats(path, [total * 0.88, total * 0.96])
    if not head or not tail:
        return None
    return head, tail


def _match_gains(src, target) -> tuple[list[float], list[float]]:
    gains, offsets = [], []
    for c in range(3):
        mean, std = src[0][c], src[1][c]
        tmean, tstd = target[0][c], target[1][c]
        gain = 1.0 if std < 1.0 else min(max(tstd / std, _MIN_GAIN), _MAX_GAIN)
        gain = 1.0 + _GRADE_STRENGTH * (gain - 1.0)
        shift = min(max(tmean - mean, -_MAX_MEAN_SHIFT), _MAX_MEAN_SHIFT)
        gains.append(gain)
        offsets.append(_GRADE_STRENGTH * shift)
    return gains, offsets


def _grade_filter(src, gains: list[float], offsets: list[float]) -> str | None:
    params = []
    for c, label in enumerate(("r", "g", "b")):
        mean, std = src[0][c], src[1][c]
        lo_in = min(max(mean - 2.0 * std, 0.0), 255.0)
        hi_in = min(max(mean + 2.0 * std, 0.0), 255.0)
        if hi_in - lo_in < 12.0:
            continue
        lo_out = min(max(gains[c] * lo_in + offsets[c], 0.0), 255.0)
        hi_out = min(max(gains[c] * hi_in + offsets[c], 0.0), 255.0)
        if hi_out - lo_out < 12.0:
            continue
        params.extend([
            f"{label}imin={lo_in / 255:.4f}", f"{label}imax={hi_in / 255:.4f}",
            f"{label}omin={lo_out / 255:.4f}", f"{label}omax={hi_out / 255:.4f}",
        ])
    return "colorlevels=" + ":".join(params) if params else None


def _apply_match(stats, gains: list[float], offsets: list[float]):
    return (
        [g * m + o for m, g, o in zip(stats[0], gains, offsets)],
        [g * s for s, g in zip(stats[1], gains)],
    )


async def _has_audio(path: Path) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
            "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except FileNotFoundError:
        return False
    return bool(out.strip())


async def collect_shot_assets(session, project_id: int) -> list[MediaAsset]:
    stmt = (
        select(Shot, Episode)
        .join(Episode, Shot.episode_id == Episode.id)
        .where(Episode.project_id == project_id)
    )
    rows = list((await session.execute(stmt)).all())
    rows.sort(key=lambda row: (row[1].order_index, row[0].order_index))

    assets: list[MediaAsset] = []
    for shot, _episode in rows:
        if not shot.video_media_id:
            continue
        asset = await session.get(MediaAsset, shot.video_media_id)
        if asset and Path(asset.path).exists():
            assets.append(asset)
    return assets


async def export_project(session, task) -> dict:
    project = await session.get(Project, task.project_id) if task.project_id else None
    if not project:
        raise ProviderError("项目不存在，无法导出")

    total_shots = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Shot)
                .join(Episode, Shot.episode_id == Episode.id)
                .where(Episode.project_id == project.id)
            )
        ).scalar_one()
    )
    if not total_shots:
        raise ProviderError("该项目暂无分镜，请先生成分镜与镜头视频")

    assets = await collect_shot_assets(session, project.id)
    if len(assets) < total_shots:
        # 缺镜头还导出，等于把上游失败包装成「成片成功」，用户要等导出完才发现少了一段
        raise ProviderError(
            f"还有 {total_shots - len(assets)} 个镜头没有可用的视频（未生成或文件缺失），"
            "已拒绝导出。请先补齐这些镜头，再重新导出完整成片"
        )

    width, height = _TARGET_SIZE.get(project.aspect_ratio, _TARGET_SIZE["9:16"])
    work_dir = storage.media_root() / f"_export/{uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)

    cut_stats = [await _probe_cut_stats(Path(a.path), a.duration_seconds) for a in assets]
    grades: list[str | None] = []
    carry: tuple | None = None
    for stats in cut_stats:
        if not stats:
            grades.append(None)
            carry = None
            continue
        head, tail = stats
        if carry is None:
            grades.append(None)
            carry = tail
            continue
        gains, offsets = _match_gains(head, carry)
        grades.append(_grade_filter(head, gains, offsets))
        carry = _apply_match(tail, gains, offsets)

    audio_flags = [await _has_audio(Path(a.path)) for a in assets]
    keep_audio = any(audio_flags)

    out_path = work_dir / "output.mp4"
    cmd = ["ffmpeg", "-y"]
    for asset in assets:
        cmd.extend(["-i", str(asset.path)])
    chains = []
    for index, (asset, grade) in enumerate(zip(assets, grades)):
        base = (
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
        )
        if grade:
            base += f",{grade}"
        chains.append(f"{base},format=yuv420p[v{index}]")
        if keep_audio:
            if audio_flags[index]:
                chains.append(
                    f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
            else:
                duration = asset.duration_seconds or 4.0
                chains.append(
                    f"anullsrc=r=48000:cl=stereo:d={duration:.3f},asetpts=PTS-STARTPTS[a{index}]"
                )
    video_inputs = "".join(f"[v{i}]" for i in range(len(assets)))
    if keep_audio:
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(assets)))
        chains.append(f"{concat_inputs}concat=n={len(assets)}:v=1:a=1[outv][outa]")
        cmd.extend([
            "-filter_complex", ";".join(chains), "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            str(out_path),
        ])
    else:
        chains.append(f"{video_inputs}concat=n={len(assets)}:v=1:a=0[outv]")
        cmd.extend([
            "-filter_complex", ";".join(chains), "-map", "[outv]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _stdout, stderr = await proc.communicate()
    except FileNotFoundError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ProviderError("未检测到 ffmpeg，请确认运行环境已安装") from exc

    if proc.returncode != 0 or not out_path.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
        raise ProviderError(f"FFmpeg 合成失败：{stderr.decode('utf-8', 'ignore')[-600:]}")

    data = out_path.read_bytes()
    shutil.rmtree(work_dir, ignore_errors=True)

    asset = await storage.persist_media(
        session,
        user_id=task.user_id,
        kind="video",
        project_id=project.id,
        task_id=task.id,
        data=data,
        mime_type="video/mp4",
        note="成片导出",
        meta={"shot_count": len(assets), "resolution": f"{width}x{height}"},
        duration_seconds=round(sum(a.duration_seconds or 0.0 for a in assets), 2),
    )
    return {"media_id": asset.id, "url": asset.url, "shot_count": len(assets)}
