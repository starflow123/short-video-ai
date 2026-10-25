"""多段素材智能剪辑：探测时长 → 场景切分 → AI 选段 → ffmpeg 合成。

不依赖语音识别：AI 只能看到片段的时长、位置和用户填写的备注，
因此备注（如「这段是打斗」「这段是空镜」）是它判断内容的主要依据。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import MediaAsset
from app.models.task import GenerationTask
from app.services import storage
from app.services.providers.base import ProviderError

# 单条素材最长处理时长，避免误传超长文件把 worker 卡死
_MAX_CLIP_SECONDS = 900.0
_MIN_SEGMENT = 1.2
_MAX_SEGMENTS_PER_CLIP = 80
_TARGET_TOLERANCE = 0.8

_RATIO_SIZE = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "1:1": (1080, 1080),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
}


@dataclass
class ClipInfo:
    index: int
    media_id: int
    note: str
    path: Path
    duration: float
    has_audio: bool

    @property
    def label(self) -> str:
        return f"素材{self.index}" + (f"（{self.note}）" if self.note else "")


@dataclass
class Segment:
    clip: int
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


async def _run(args: list[str], timeout: float = 600) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ProviderError("未检测到 ffmpeg，请确认运行环境已安装 ffmpeg") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ProviderError("视频处理超时，请缩短素材或减少片段数量") from exc
    return proc.returncode, out, err


async def probe_video(path: Path) -> tuple[float, int, int, bool]:
    """返回时长 / 宽 / 高 / 是否带音轨。"""
    code, out, err = await _run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        timeout=60,
    )
    if code != 0:
        raise ProviderError(f"无法读取视频信息：{err.decode('utf-8', 'ignore')[-300:]}")
    try:
        payload = json.loads(out or b"{}")
    except json.JSONDecodeError as exc:
        raise ProviderError("视频信息解析失败") from exc

    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    width = height = 0
    has_audio = False
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") == "audio":
            has_audio = True
        if stream.get("codec_type") == "video":
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if not duration:
                duration = float(stream.get("duration") or 0.0)
    if duration <= 0:
        raise ProviderError("视频时长为 0，文件可能已损坏")
    return duration, width, height, has_audio


async def detect_segments(path: Path, duration: float) -> list[Segment]:
    """用 ffmpeg 场景变化检测切分；检测不到切点时按 4 秒均分。"""
    code, _out, err = await _run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-filter:v", "select='gt(scene,0.35)',showinfo",
            "-an", "-f", "null", "-",
        ],
        timeout=240,
    )
    text = err.decode("utf-8", "ignore")
    if code != 0:
        raise ProviderError(f"场景检测失败：{text[-300:]}")
    cuts = sorted({
        round(float(value), 2)
        for value in re.findall(r"Parsed_showinfo.*?pts_time:([0-9.]+)", text)
        if 0.4 < float(value) < duration - 0.4
    })

    bounds = [0.0, *cuts, duration]
    segments: list[Segment] = []
    for index in range(len(bounds) - 1):
        start, end = bounds[index], bounds[index + 1]
        if end - start < _MIN_SEGMENT and segments:
            # 太碎的镜头并进前一段，避免成片节奏碎成 PPT
            segments[-1] = Segment(clip=0, index=segments[-1].index, start=segments[-1].start, end=end)
            continue
        segments.append(Segment(clip=0, index=len(segments), start=start, end=end))

    if not segments:
        step = max(2.0, duration / max(1, int(duration // 4) + 1))
        start = 0.0
        while start < duration - 0.3:
            end = min(duration, start + step)
            segments.append(Segment(clip=0, index=len(segments), start=start, end=end))
            start = end

    if len(segments) > _MAX_SEGMENTS_PER_CLIP:
        segments = segments[:_MAX_SEGMENTS_PER_CLIP]
    return segments


async def _step(session: AsyncSession, task: GenerationTask, value: int) -> None:
    task.progress = max(0, min(99, value))
    await session.commit()


def _plan_prompt(
    clips: list[ClipInfo],
    segments: dict[int, list[Segment]],
    *,
    target: float,
    pace: str,
    instruction: str | None,
) -> str:
    lines = [
        f"目标成片时长：{target:.0f} 秒",
        f"节奏：{pace}",
        f"用户要求：{instruction or '无'}",
        "",
        "可用素材与片段（时间单位秒）：",
    ]
    for clip in clips:
        lines.append(f"{clip.label}，总长 {clip.duration:.1f}s")
        for seg in segments[clip.index]:
            lines.append(
                f"  - 素材{clip.index} 片段{seg.index}：{seg.start:.1f}-{seg.end:.1f}s"
                f"（{seg.duration:.1f}s）"
            )
    lines.extend(
        [
            "",
            "请挑选并按成片顺序排列片段，使总时长尽量接近目标时长（误差不超过 1 秒）。",
            "只能使用上面列出的素材编号与片段编号，不要发明时间点。",
            "只输出 JSON："
            '{"picks":[{"clip":1,"segment":0}],"title":"成片标题"}',
        ]
    )
    return "\n".join(lines)


_EDIT_SYSTEM = (
    "你是短剧剪辑师。根据用户提供的素材备注、片段时长与要求，挑选并排序片段。"
    "你只能决定用哪些片段、按什么顺序，不能改写时间点，也不能编造不存在的片段。"
    "优先保持叙事连贯：同一动作的连续片段尽量相邻；空镜用于转场或留白；"
    "总时长必须接近目标值，宁少勿超。只输出 JSON。"
)


async def _ai_picks(
    session: AsyncSession,
    *,
    user_id: int,
    clips: list[ClipInfo],
    segments: dict[int, list[Segment]],
    target: float,
    pace: str,
    instruction: str | None,
) -> tuple[list[Segment], str | None]:
    """让文本模型选段；无可用模型时返回空列表，由确定性兜底接管。"""
    try:
        from app.services.generation import (
            _generate_json,
            build_provider_from,
            resolve_credential,
        )

        credential = await resolve_credential(session, user_id, "text", None, None)
        provider = build_provider_from(credential)
        raw = await _generate_json(
            provider,
            _plan_prompt(clips, segments, target=target, pace=pace, instruction=instruction),
            system=_EDIT_SYSTEM,
            model=None,
            temperature=0.3,
            max_tokens=4096,
        )
    except Exception:  # noqa: BLE001 - AI 选段失败时不能整单失败
        return [], None

    by_key = {
        (clip.index, seg.index): seg for clip in clips for seg in segments[clip.index]
    }
    picked: list[Segment] = []
    seen: set[tuple[int, int]] = set()
    for item in raw.get("picks") or []:
        try:
            key = (int(item.get("clip")), int(item.get("segment")))
        except (TypeError, ValueError):
            continue
        seg = by_key.get(key)
        if not seg or key in seen:
            continue
        seen.add(key)
        picked.append(seg)
        if sum(seg.duration for seg in picked) > target + _TARGET_TOLERANCE:
            break
    title = str(raw.get("title") or "").strip() or None
    return picked, title


def _fallback_picks(segments: dict[int, list[Segment]], target: float) -> list[Segment]:
    """模型不可用时的保底：按素材顺序、片段顺序取，凑够目标时长。"""
    picked: list[Segment] = []
    total = 0.0
    for clip_segments in segments.values():
        for seg in clip_segments:
            if total >= target:
                return picked
            picked.append(seg)
            total += seg.duration
    return picked


def _fit_target(picked: list[Segment], target: float) -> list[Segment]:
    """把选段压到目标时长：最后一段裁短；不足 0.8 秒的边角直接丢掉。"""
    result: list[Segment] = []
    total = 0.0
    for seg in picked:
        remain = target - total
        if remain <= 0.4:
            break
        if seg.duration <= remain + _TARGET_TOLERANCE:
            result.append(seg)
            total += seg.duration
        else:
            cut = seg.start + remain
            if cut - seg.start >= 0.8:
                result.append(Segment(clip=seg.clip, index=seg.index, start=seg.start, end=cut))
                total += cut - seg.start
            break
    return result


def _pad_picks(
    picked: list[Segment], segments: dict[int, list[Segment]], target: float
) -> list[Segment]:
    """AI 选得太少时补足：按素材顺序追加未选中的片段，避免成片只有几秒。"""
    total = sum(seg.duration for seg in picked)
    if total >= target * 0.6:
        return picked
    used = {(seg.clip, seg.index) for seg in picked}
    for clip_segments in segments.values():
        for seg in clip_segments:
            if total >= target:
                return picked
            if (seg.clip, seg.index) in used:
                continue
            picked.append(seg)
            used.add((seg.clip, seg.index))
            total += seg.duration
    return picked


async def _render(
    session: AsyncSession,
    task: GenerationTask,
    *,
    clips: dict[int, ClipInfo],
    picks: list[Segment],
    aspect_ratio: str,
    out_path: Path,
    workdir: Path,
) -> None:
    width, height = _RATIO_SIZE.get(aspect_ratio, _RATIO_SIZE["9:16"])
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30"
    )

    parts: list[Path] = []
    total = len(picks)
    for order, seg in enumerate(picks):
        clip = clips[seg.clip]
        duration = max(0.4, seg.duration)
        part = workdir / f"part-{order:03d}.mp4"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seg.start:.3f}", "-i", str(clip.path),
        ]
        if clip.has_audio:
            maps = ["-map", "0:v:0", "-map", "0:a:0"]
            audio_filter = ["-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"]
        else:
            # 无音轨素材补静音，保证 concat 时所有分段流结构一致
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            maps = ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
            audio_filter = []
        cmd += ["-t", f"{duration:.3f}", "-vf", vf, *audio_filter, *maps,
                "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-ar", "48000", "-ac", "2", str(part)]
        code, _out, err = await _run(cmd, timeout=300)
        if code != 0 or not part.exists():
            raise ProviderError(f"第 {order + 1} 段裁切失败：{err.decode('utf-8', 'ignore')[-300:]}")
        parts.append(part)
        await _step(session, task, 40 + int((order + 1) / max(1, total) * 45))

    if not parts:
        raise ProviderError("没有可用的片段，无法合成")

    list_file = workdir / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{part.as_posix()}'" for part in parts) + "\n", encoding="utf-8"
    )
    code, _out, err = await _run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", "-movflags", "+faststart", str(out_path),
        ],
        timeout=300,
    )
    if code != 0 or not out_path.exists():
        raise ProviderError(f"成片合成失败：{err.decode('utf-8', 'ignore')[-300:]}")


async def execute_edit(session: AsyncSession, task: GenerationTask) -> dict[str, Any]:
    params = task.params or {}
    raw_clips = params.get("clips") or []
    target = float(params.get("target_duration") or 30)
    aspect_ratio = str(params.get("aspect_ratio") or "9:16")
    pace = str(params.get("pace") or "medium")
    instruction = params.get("instruction") or None

    clips: dict[int, ClipInfo] = {}
    for index, item in enumerate(raw_clips, start=1):
        media = await session.get(MediaAsset, int(item.get("media_id") or 0))
        if not media or media.user_id != task.user_id or media.kind != "video":
            raise ProviderError(f"素材 {item.get('media_id')} 不存在或不可用")
        path = Path(media.path)
        if not path.exists():
            raise ProviderError(f"素材文件缺失：{media.filename}")
        duration, _w, _h, has_audio = await probe_video(path)
        if duration > _MAX_CLIP_SECONDS:
            raise ProviderError(
                f"素材 {media.filename} 时长 {duration:.0f}s 超过上限 {_MAX_CLIP_SECONDS:.0f}s"
            )
        clips[index] = ClipInfo(
            index=index,
            media_id=media.id,
            note=str(item.get("note") or "").strip(),
            path=path,
            duration=duration,
            has_audio=has_audio,
        )

    await _step(session, task, 12)
    segments: dict[int, list[Segment]] = {}
    for index, clip in clips.items():
        found = await detect_segments(clip.path, clip.duration)
        for seg in found:
            seg.clip = index
        segments[index] = found
    await _step(session, task, 32)

    picked, title = await _ai_picks(
        session,
        user_id=task.user_id,
        clips=list(clips.values()),
        segments=segments,
        target=target,
        pace=pace,
        instruction=instruction,
    )
    if not picked:
        picked = _fallback_picks(segments, target)
    picked = _fit_target(_pad_picks(picked, segments, target), target)
    if not picked:
        raise ProviderError("没有可用的片段，无法合成")
    await _step(session, task, 38)

    workdir = storage.media_root() / ".tmp" / f"edit-{task.id}"
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = workdir / "result.mp4"
    try:
        await _render(
            session,
            task,
            clips=clips,
            picks=picked,
            aspect_ratio=aspect_ratio,
            out_path=out_path,
            workdir=workdir,
        )
        await _step(session, task, 94)
        duration, width, height, _audio = await probe_video(out_path)
        asset = await storage.persist_media(
            session,
            user_id=task.user_id,
            kind="video",
            data=out_path.read_bytes(),
            mime_type="video/mp4",
            task_id=task.id,
            duration_seconds=duration,
            source="edited",
            meta={
                "edit": {
                    "target_duration": target,
                    "aspect_ratio": aspect_ratio,
                    "pace": pace,
                    "picks": [
                        {"clip": seg.clip, "start": seg.start, "end": seg.end} for seg in picked
                    ],
                },
                "width": width,
                "height": height,
            },
            note=title or instruction,
        )
    finally:
        # 临时文件全部放在 media 卷里（D 盘），处理完即清理
        for path in sorted(workdir.glob("*"), reverse=True):
            path.unlink(missing_ok=True)
        workdir.rmdir()

    return {
        "media_id": asset.id,
        "url": asset.url,
        "duration_seconds": duration,
        "title": title,
        "segments": len(picked),
        "aspect_ratio": aspect_ratio,
    }
