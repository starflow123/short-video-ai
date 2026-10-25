"""切点连续性审查：上一镜尾帧 vs 下一镜首帧，只看「人 / 服装 / 场景 / 光线」是否崩。

硬约束：
- 只给建议，不阻断导出（dry-run 阶段）；模型不可用 / 抽帧失败时跳过，不把导出标记为失败；
- 姿态突变、景别切换属于正常剪辑，不算 fail（用户已明确这是已知边界）；
- 结论写进下一镜视频素材的 ``meta.continuity_review``，视频不变时复用上次结果。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.episode import Episode
from app.models.media import MediaAsset
from app.models.shot import Shot
from app.services import prompts
from app.services.keyframes import load_keyframes
from app.services.providers.base import ProviderError
from app.services.storage import asset_bytes

logger = logging.getLogger(__name__)

CHECK_LABELS: dict[str, str] = {
    "same_person": "是不是同一人",
    "costume_continuity": "服装发型连续",
    "scene_continuity": "场景连续",
    "lighting_continuity": "光线色温连续",
    "keyframe_match": "首帧贴合认可帧",
}
_ALLOWED = {"pass", "warn", "fail", "unknown"}
# 720 宽足够辨认五官与服装，同时把单张图 token 压到约三分之一
_FRAME_WIDTH = 720
_FFMPEG_TIMEOUT = 60
# 整项目审查的总时间预算：模型异常时宁可少审几个切点，也不能把导出拖到几十分钟
_BUDGET_SECONDS = 150

_SYSTEM = (
    "你是短剧剪辑的连续性审查员。只比较给出的帧，判断镜头切换处人物、服装、"
    "场景、光线是否崩坏；不评价画风美丑，不要求动作或景别保持一致。"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _subjects(shot: Shot) -> list[str]:
    lines: list[str] = []
    for subject in _as_list(shot.subjects)[:6]:
        if not isinstance(subject, dict):
            continue
        name = _text(subject.get("name"))
        if not name:
            continue
        bits = [name]
        for key, label in (("appearance", "外观"), ("costume", "服装"), ("holds", "手持")):
            value = _text(subject.get(key))
            if value:
                bits.append(f"{label}：{value}")
        lines.append("｜".join(bits))
    return lines


def _directives(shot: Shot) -> str:
    data = shot.directives if isinstance(shot.directives, dict) else {}
    bits = [
        f"{label}：{_text(data.get(key))}"
        for key, label in (("shot_size", "景别"), ("camera_angle", "机位"), ("composition", "构图"))
        if _text(data.get(key))
    ]
    return "；".join(bits)


def _state_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return "；".join(f"{key}：{_text(item)}" for key, item in value.items() if _text(item))[:300]


def _shot_block(shot: Shot, *, label: str) -> list[str]:
    lines = [f"【{label}】第 {shot.order_index} 镜《{_text(shot.title) or '未命名'}》"]
    if _text(shot.description):
        lines.append(f"画面：{_text(shot.description)[:400]}")
    directives = _directives(shot)
    if directives:
        lines.append(f"镜头：{directives}")
    subjects = _subjects(shot)
    lines.append("出场人物：" + ("；".join(subjects) if subjects else "（无，空镜或环境镜）"))
    return lines


def build_cut_prompt(
    prev_shot: Shot, shot: Shot, *, image_labels: list[str], has_keyframe: bool
) -> str:
    sections: list[str] = [
        "请只审查这两帧之间的「剪辑切点」，严格按【判定规则】输出 JSON。",
        "【图片说明】",
        "\n".join(image_labels),
        *_shot_block(prev_shot, label="上一镜"),
        *_shot_block(shot, label="下一镜"),
    ]
    prev_end = _state_text(prev_shot.end_state)
    if prev_end:
        sections.append(f"上一镜结尾状态：{prev_end}")
    next_start = _state_text(shot.start_state)
    if next_start:
        sections.append(f"下一镜起始状态：{next_start}")

    rules = """【判定规则】
- same_person：图1 与图2 中同一个角色是否还是同一人（五官、脸型、发型、年龄段、性别）。一镜里没出现人物时给 pass；看不清给 unknown。
- costume_continuity：同一角色的服装、发型、配饰是否连续。剧情明确换装 / 换场后换装给 pass；无明显理由突然换装、少穿、颜色大改给 fail。
- scene_continuity：两镜是否处在同一时空且陈设连续。镜头描述明确换场（时间 / 地点变化）时给 pass；没有换场依据但背景地点或主要陈设突然改变给 fail。
- lighting_continuity：主光方向、色温、明暗是否连续。轻微色差由导出时做色彩匹配，最多 warn；明显昼夜反转或冷暖颠倒给 fail；明确换场给 pass。
- keyframe_match：图2 是否与图3（下一镜已认可的关键帧）是同一场景、同一角色、同一构图起点。没有图3时给 unknown；手位、表情、呼吸等细微差异是视频运动造成的，不算问题，不要因此给 warn；只有人物 / 服装 / 场景 / 构图明显不是同一张画面时才给 warn 或 fail。
- 姿态突变、动作推进、景别 / 机位切换本身是正常剪辑，一律不算问题。
- 只报你能从图中确认的事实；看不清给 unknown，不要猜测。
"""
    sections.append(rules)
    sections.append(
        """【输出】
{"verdict":"pass|warn|fail","checks":{"same_person":"pass|warn|fail|unknown","costume_continuity":"...","scene_continuity":"...","lighting_continuity":"...","keyframe_match":"..."},"reasons":["最多 5 条，只写不合格或不确定的项，写清是哪个角色 / 哪一帧、错在哪；合格项不要写"]}
verdict 规则：任一项 fail => fail；没有 fail 但有 warn/unknown => warn；全部 pass => pass。"""
    )
    if not has_keyframe:
        sections.append("本次没有提供图3（下一镜没有已认可关键帧），keyframe_match 固定给 unknown，不要臆测。")
    return "\n".join(sections)


def _derive_verdict(checks: dict[str, str], model_verdict: str) -> str:
    values = set(checks.values())
    if "fail" in values or model_verdict == "fail":
        return "fail"
    if "warn" in values or "unknown" in values:
        return "warn"
    return "pass"


def normalize_cut(
    data: dict,
    *,
    provider: str,
    model: str,
    from_media_id: int,
    to_media_id: int,
    from_shot_id: int,
    to_shot_id: int,
    has_keyframe: bool,
    subject_count: int | None = None,
) -> dict:
    raw_checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    checks: dict[str, str] = {}
    for key in CHECK_LABELS:
        value = _text(raw_checks.get(key)).lower()
        checks[key] = value if value in _ALLOWED else "unknown"

    # 没有图3 就没有可比对的关键帧；这不是风险，避免每个切点都挂 warn
    if not has_keyframe and checks["keyframe_match"] == "unknown":
        checks["keyframe_match"] = "pass"
    # 两端都是空镜时没人可认，人物项不适用
    if subject_count == 0:
        for key in ("same_person", "costume_continuity"):
            if checks[key] == "unknown":
                checks[key] = "pass"

    model_verdict = _text(data.get("verdict")).lower()
    if model_verdict not in {"pass", "warn", "fail"}:
        model_verdict = ""
    verdict = _derive_verdict(checks, model_verdict)

    reasons: list[str] = []
    for item in data.get("reasons") or []:
        text = _text(item)
        if text:
            reasons.append(text[:200])
        if len(reasons) >= 5:
            break

    return {
        "verdict": verdict,
        "checks": checks,
        "reasons": reasons,
        "provider": provider,
        "model": model,
        "from_shot_id": from_shot_id,
        "to_shot_id": to_shot_id,
        "from_media_id": from_media_id,
        "to_media_id": to_media_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # AI 只给建议；是否重跑镜头仍由人决定
        "advisory": True,
    }


async def _extract_frame(path: Path, *, at_end: bool) -> bytes:
    """用 ffmpeg 抽首帧 / 尾帧（尾帧按文件末尾定位，不依赖时长字段是否准确）。"""
    seek = ["-sseof", "-0.30"] if at_end else ["-ss", "0.05"]
    cmd = [
        "ffmpeg", "-v", "error", *seek, "-i", str(path),
        "-frames:v", "1", "-vf", f"scale={_FRAME_WIDTH}:-2",
        "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ProviderError("未检测到 ffmpeg，无法抽取视频帧") from exc
    try:
        raw, err = await asyncio.wait_for(proc.communicate(), timeout=_FFMPEG_TIMEOUT)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ProviderError("抽取视频帧超时") from exc
    if proc.returncode != 0 or not raw:
        detail = err.decode("utf-8", "ignore")[-300:]
        raise ProviderError(f"抽取视频帧失败：{detail or '输出为空'}")
    return raw


async def previous_shot(session: AsyncSession, shot: Shot) -> Shot | None:
    """按成片顺序找上一镜：同集内前一镜，没有就取上一集的最后一镜。"""
    episode = await session.get(Episode, shot.episode_id)
    if episode is None:
        return None
    prev = (
        await session.execute(
            select(Shot)
            .where(
                Shot.episode_id == shot.episode_id,
                or_(
                    Shot.order_index < shot.order_index,
                    and_(Shot.order_index == shot.order_index, Shot.id < shot.id),
                ),
            )
            .order_by(Shot.order_index.desc(), Shot.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if prev is not None:
        return prev
    prev_episode = (
        await session.execute(
            select(Episode)
            .where(
                Episode.project_id == episode.project_id,
                Episode.order_index < episode.order_index,
            )
            .order_by(Episode.order_index.desc())
            .limit(1)
        )
    ).scalars().first()
    if prev_episode is None:
        return None
    return (
        await session.execute(
            select(Shot)
            .where(Shot.episode_id == prev_episode.id)
            .order_by(Shot.order_index.desc(), Shot.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def _video_asset(session: AsyncSession, shot: Shot) -> MediaAsset | None:
    if not shot.video_media_id:
        return None
    asset = await session.get(MediaAsset, int(shot.video_media_id))
    if asset is None or not Path(asset.path).exists():
        return None
    return asset


async def _keyframe_image(session: AsyncSession, shot: Shot) -> bytes | None:
    """下一镜已认可的关键帧：用来核对视频首帧是否还在认可画面上。"""
    keyframes = (await load_keyframes(session, [shot.id])).get(shot.id, [])
    approved = next(
        (item for item in reversed(keyframes) if item.approved_at and item.media_id), None
    )
    if approved is None:
        return None
    media = await session.get(MediaAsset, int(approved.media_id))
    if media is None:
        return None
    try:
        return await asset_bytes(media)
    except Exception as exc:  # noqa: BLE001 - 关键帧缺失只影响一项检查
        logger.warning("切点审查读不到关键帧 %s：%s", approved.media_id, exc)
        return None


def _fresh(asset: MediaAsset, prev_asset: MediaAsset) -> bool:
    review = (asset.meta or {}).get("continuity_review")
    if not isinstance(review, dict):
        return False
    return (
        review.get("from_media_id") == prev_asset.id
        and review.get("to_media_id") == asset.id
    )


async def review_cut(
    session: AsyncSession,
    *,
    user_id: int,
    prev_shot: Shot,
    shot: Shot,
    force: bool = False,
) -> dict | None:
    """审查一个切点；视频未变化且已有结论时直接复用（``force`` 可强制重跑）。"""
    from app.services.generation import build_provider_from, resolve_credential

    prev_asset = await _video_asset(session, prev_shot)
    asset = await _video_asset(session, shot)
    if prev_asset is None or asset is None:
        raise ProviderError("相邻镜头缺少可用视频文件，无法审查切点")
    if not force and _fresh(asset, prev_asset):
        return (asset.meta or {}).get("continuity_review")

    credential = await resolve_credential(session, user_id, "vision")
    provider = build_provider_from(credential)
    if not provider.supports("vision"):
        raise ValueError(f"当前视觉凭证（{credential.provider}）不支持读图审查")

    prev_frame = await _extract_frame(Path(prev_asset.path), at_end=True)
    next_frame = await _extract_frame(Path(asset.path), at_end=False)
    keyframe = await _keyframe_image(session, shot)

    images: list[tuple[str, bytes]] = [
        ("上一镜最后一帧", prev_frame),
        ("下一镜第一帧", next_frame),
    ]
    labels = ["图1：上一镜最后一帧", "图2：下一镜第一帧"]
    if keyframe:
        images.append(("下一镜已认可的关键帧", keyframe))
        labels.append("图3：下一镜已认可的关键帧（视频首帧应对上这张）")

    prompt = build_cut_prompt(prev_shot, shot, image_labels=labels, has_keyframe=bool(keyframe))
    raw = await provider.generate_vision(
        prompt,
        images=images,
        system=_SYSTEM,
        json_mode=True,
        max_tokens=800,
    )
    try:
        data = prompts.extract_json(raw)
    except ValueError as exc:
        raise ValueError(f"视觉模型返回无法解析：{raw[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"视觉模型返回格式不正确：{raw[:200]}")

    report = normalize_cut(
        data,
        provider=credential.provider,
        model=provider.model_for("vision"),
        from_media_id=prev_asset.id,
        to_media_id=asset.id,
        from_shot_id=prev_shot.id,
        to_shot_id=shot.id,
        has_keyframe=bool(keyframe),
        subject_count=len(_as_list(prev_shot.subjects)) + len(_as_list(shot.subjects)),
    )
    asset.meta = {**(asset.meta or {}), "continuity_review": report}
    await session.flush()
    return report


async def review_project(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    force: bool = False,
) -> dict:
    """按成片顺序审查项目内所有相邻切点（含跨集切点）；单点失败只记录不抛出。"""
    rows = list(
        (
            await session.execute(
                select(Shot, Episode)
                .join(Episode, Shot.episode_id == Episode.id)
                .where(Episode.project_id == project_id)
                .order_by(Episode.order_index, Shot.order_index, Shot.id)
            )
        ).all()
    )
    # 只留 ID：中间会 commit / rollback，ORM 对象可能过期，循环里按 ID 重新取
    shot_ids = [int(shot.id) for shot, _episode in rows if shot.video_media_id]

    started = time.monotonic()
    reports: list[dict] = []
    skipped: list[dict] = []
    reused = 0
    for prev_id, shot_id in zip(shot_ids, shot_ids[1:]):
        if time.monotonic() - started > _BUDGET_SECONDS:
            skipped.append({"to_shot_id": shot_id, "error": "超出本次审查时间预算，已跳过"})
            continue
        try:
            prev_shot = await session.get(Shot, prev_id)
            shot = await session.get(Shot, shot_id)
            if prev_shot is None or shot is None:
                continue
            prev_asset = await _video_asset(session, prev_shot)
            asset = await _video_asset(session, shot)
            fresh = (
                not force
                and prev_asset is not None
                and asset is not None
                and _fresh(asset, prev_asset)
            )
            report = await review_cut(
                session, user_id=user_id, prev_shot=prev_shot, shot=shot, force=force
            )
            await session.commit()
            if report:
                reports.append(report)
                if fresh:
                    reused += 1
        except Exception as exc:  # noqa: BLE001 - 审查失败不能影响导出
            await session.rollback()
            logger.warning("切点 %s→%s 审查跳过：%s", prev_id, shot_id, exc)
            skipped.append(
                {"from_shot_id": prev_id, "to_shot_id": shot_id, "error": str(exc)[:200]}
            )

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for report in reports:
        counts[report["verdict"]] = counts.get(report["verdict"], 0) + 1
    return {
        "cuts": len(reports),
        "reused": reused,
        "verdicts": counts,
        "reports": [
            {
                "from_shot_id": item["from_shot_id"],
                "to_shot_id": item["to_shot_id"],
                "verdict": item["verdict"],
                "reasons": item["reasons"],
            }
            for item in reports
        ],
        "skipped": skipped,
        "advisory": True,
    }


async def maybe_review_project(session: AsyncSession, *, user_id: int, project_id: int) -> dict | None:
    """自动入口（导出前）：开关关闭或厂商异常时静默跳过，绝不阻断导出。"""
    if not settings.continuity_review_enabled:
        return None
    try:
        return await review_project(session, user_id=user_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - 审查是增强能力，绝不能让导出失败
        await session.rollback()
        logger.warning("项目 %s 切点审查跳过：%s", project_id, exc)
        return None
