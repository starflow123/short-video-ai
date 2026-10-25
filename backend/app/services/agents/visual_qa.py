"""关键帧视觉初审：读图核对「像不像本人 / 服装 / 人数 / 道具 / 分身 / 构图」。

硬约束：
- 只给建议，永远不自动点「认可」，也不阻断生图 / 生视频；
- 模型不可用、超时、返回脏数据时静默跳过，不把主任务标记为失败；
- 结论写进 ``media_assets.meta.visual_review``，与 ``reference_*`` 同路，无需迁移。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.asset import ProductionAsset
from app.models.episode import Episode
from app.models.media import MediaAsset
from app.models.shot import Shot
from app.services import prompts, storage

logger = logging.getLogger(__name__)

CHECK_LABELS: dict[str, str] = {
    "face_match": "像不像本人",
    "costume_match": "服装一致",
    "cast_count": "出场人数",
    "prop_holder": "道具归属",
    "duplicate_person": "有无分身",
    "composition": "构图景别",
}
_ALLOWED = {"pass", "warn", "fail", "unknown"}
_MAX_SHEETS = 3

_SYSTEM = (
    "你是严谨的短剧关键帧审查员。只依据图片与给出的分镜要求判断，"
    "不臆测看不清的内容，不评价画风美丑，也不重写提示词。"
)


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm_name(value: Any) -> str:
    return _text(value).replace("（", "(").replace("）", ")").replace(" ", "").lower()


def _subject_lines(subjects: list) -> list[str]:
    lines: list[str] = []
    for subject in subjects[:6]:
        if not isinstance(subject, dict):
            continue
        name = _text(subject.get("name"))
        if not name:
            continue
        parts = [name]
        for key, label in (
            ("appearance", "外观"),
            ("costume", "服装"),
            ("action", "动作"),
            ("expression", "神态"),
            ("holds", "手持"),
            ("position", "位置"),
        ):
            value = _text(subject.get(key))
            if value:
                parts.append(f"{label}：{value}")
        lines.append("｜".join(parts))
    return lines


def _prop_lines(props: list) -> list[str]:
    lines: list[str] = []
    for prop in props[:6]:
        if not isinstance(prop, dict):
            continue
        name = _text(prop.get("name"))
        if not name:
            continue
        owner = _text(prop.get("owner"))
        state = _text(prop.get("state"))
        line = f"{name}"
        if owner:
            line += f"（应由 {owner} 持有）"
        if state:
            line += f"，状态：{state}"
        lines.append(line)
    return lines


def build_review_prompt(
    shot: Shot, *, image_labels: list[str], actual_prompt: str | None = None
) -> str:
    subjects = _as_list(shot.subjects)
    props = _as_list(shot.props)
    directives = shot.directives if isinstance(shot.directives, dict) else {}
    camera_bits = [
        f"{label}：{_text(directives.get(key))}"
        for key, label in (
            ("shot_size", "景别"),
            ("camera_angle", "机位"),
            ("camera_movement", "运动"),
            ("composition", "构图"),
        )
        if _text(directives.get(key))
    ]
    if _text(shot.camera):
        camera_bits.append(f"机位：{_text(shot.camera)}")

    if subjects:
        cast_rule = f"出场角色（画面应恰好出现 {len(subjects)} 个可辨认人物）"
    else:
        cast_rule = "本镜为空镜：不应出现可辨认人物"
    cast_block = "\n".join(f"{index}. {line}" for index, line in enumerate(_subject_lines(subjects), 1))
    prop_block = "\n".join(f"- {line}" for line in _prop_lines(props))
    image_block = "\n".join(image_labels)

    sections = [
        "请审查图1这张关键帧，严格按【判定规则】输出 JSON。",
        "【本镜要求】",
        f"标题：{_text(shot.title) or '未命名'}",
        f"画面描述：{_text(shot.description) or '（未填写）'}",
    ]
    if camera_bits:
        sections.append("镜头要求：" + "；".join(camera_bits))
    sections.append(cast_rule + "：")
    sections.append(cast_block or "（未建档，按画面描述判断）")
    if prop_block:
        sections.append("道具归属：")
        sections.append(prop_block)
    sections.append("".join(["【图片说明】", "\n", image_block]))
    # 优先用这一帧实际发送的干净提示词：用户可能在生成时单独改过，与 shot.image_prompt 不同
    used_prompt = _text(actual_prompt) or _text(shot.image_prompt)
    if used_prompt:
        sections.append("【生成时使用的画面提示词（仅供对照）】")
        sections.append(used_prompt[:600])
    sections.append(
        """【判定规则】
- face_match：图1中的角色与对应定妆照是否为同一人（五官、脸型、发型、年龄段、性别）。没有定妆照可比对时给 unknown。
- costume_match：服装 / 发型 / 配饰是否与本镜要求一致；换装、少穿、颜色明显不符给 fail。
- cast_count：图1中可辨认的人物数量是否等于出场角色数。多出的人（含路人、被复制的人）算问题；远景虚化人群不计。
- prop_holder：本镜道具是否出现、是否由指定角色持有；道具缺失或换人持有给 fail。
- duplicate_person：同一角色是否在图1中出现两次（镜面、倒影、照片里的影像不算）。
- composition：是否明显违反景别、机位或构图要求；不评价画风美丑。
- 本镜为空镜时：face_match / costume_match / duplicate_person 不适用，一律给 pass；cast_count 只检查是否出现可辨认人物。
- 只报你能从图中确认的事实；看不清给 unknown，不要猜测。

【输出】
{"verdict":"pass|warn|fail","checks":{"face_match":"pass|warn|fail|unknown","costume_match":"...","cast_count":"...","prop_holder":"...","duplicate_person":"...","composition":"..."},"reasons":["最多 5 条，只写不合格或不确定的项，写清哪个角色/道具、错在哪；合格项不要写"],"prompt_patch":"仅 warn/fail 时给一句可直接追加到画面描述的修正建议（不超过 80 字，只补视觉事实，不推翻人数/服装/道具硬约束）；pass 时为空字符串"}
verdict 规则：任一项 fail => fail；没有 fail 但有 warn/unknown => warn；全部 pass => pass。"""
    )
    return "\n".join(sections)


async def _reference_sheets(
    session: AsyncSession,
    *,
    user_id: int,
    shot: Shot,
    keyframe_media_id: int,
) -> list[tuple[str, bytes]]:
    """取本镜出场角色的定妆照，作为看图比对的参照；拿不到就跳过。"""
    subjects = [item for item in _as_list(shot.subjects) if isinstance(item, dict)]
    if not subjects:
        return []
    episode = await session.get(Episode, shot.episode_id)
    project_id = episode.project_id if episode else None
    conditions = [
        ProductionAsset.user_id == user_id,
        ProductionAsset.kind == "actor",
    ]
    if project_id:
        conditions.append(
            or_(ProductionAsset.project_id == project_id, ProductionAsset.is_global.is_(True))
        )
    else:
        conditions.append(ProductionAsset.is_global.is_(True))
    actors = list(
        (await session.execute(select(ProductionAsset).where(*conditions))).scalars().all()
    )

    picked: list[tuple[str, bytes]] = []
    used_media: set[int] = set()
    for subject in subjects:
        name = _text(subject.get("name"))
        if not name:
            continue
        match = None
        raw_id = subject.get("asset_id")
        if raw_id is not None:
            try:
                wanted = int(raw_id)
            except (TypeError, ValueError):
                wanted = None
            if wanted is not None:
                match = next((item for item in actors if item.id == wanted), None)
        if match is None:
            match = next(
                (item for item in actors if _norm_name(item.name) == _norm_name(name)), None
            )
        if not match or not match.media_id or int(match.media_id) == keyframe_media_id:
            continue
        if match.media_id in used_media:
            continue
        media = await session.get(MediaAsset, int(match.media_id))
        if media is None:
            continue
        try:
            data = await storage.asset_bytes(media)
        except Exception as exc:  # noqa: BLE001 - 参考资料缺失不应让审查失败
            logger.warning("视觉初审读不到定妆照 %s：%s", match.media_id, exc)
            continue
        picked.append((f"角色「{name}」的定妆照（比对五官/发型/服装）", data))
        used_media.add(int(match.media_id))
        if len(picked) >= _MAX_SHEETS:
            break
    return picked


def _derive_verdict(checks: dict[str, str], model_verdict: str) -> str:
    """结论以逐项检查为准；模型整体判 fail 时保留 fail（可能有检查项没覆盖到的问题）。"""
    values = set(checks.values())
    if "fail" in values or model_verdict == "fail":
        return "fail"
    if "warn" in values or "unknown" in values:
        return "warn"
    return "pass"


def normalize_review(
    data: dict,
    *,
    provider: str,
    model: str,
    media_id: int,
    subject_count: int | None = None,
) -> dict:
    raw_checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    checks: dict[str, str] = {}
    for key in CHECK_LABELS:
        value = _text(raw_checks.get(key)).lower()
        checks[key] = value if value in _ALLOWED else "unknown"

    # 空镜没有人物可比对：这两项「看不清」不代表风险，否则每张空镜都会被标成有风险
    if subject_count == 0:
        for key in ("face_match", "costume_match", "duplicate_person"):
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
        "prompt_patch": _text(data.get("prompt_patch"))[:200],
        "provider": provider,
        "model": model,
        "media_id": media_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # 给前端 / 审计看的语义：AI 只给建议，认可仍由人点
        "advisory": True,
    }


async def review_keyframe(
    session: AsyncSession,
    *,
    user_id: int,
    asset: MediaAsset,
    shot: Shot,
) -> dict:
    """对一张关键帧做视觉初审，结论写回 ``asset.meta.visual_review``。

    失败时直接抛异常，由调用方决定「自动审查静默跳过 / 手动审查报错」。
    """
    # 延迟导入：generation 在生图流程里会调用本模块，模块级互相 import 会成环
    from app.services.generation import build_provider_from, resolve_credential

    credential = await resolve_credential(session, user_id, "vision")
    provider = build_provider_from(credential)
    if not provider.supports("vision"):
        raise ValueError(f"当前视觉凭证（{credential.provider}）不支持读图审查")

    image_data = await storage.asset_bytes(asset)
    if not image_data:
        raise ValueError("关键帧文件为空，无法审查")

    sheets = await _reference_sheets(
        session, user_id=user_id, shot=shot, keyframe_media_id=int(asset.id)
    )
    images: list[tuple[str, bytes]] = [("待审关键帧", image_data), *sheets]
    labels = ["图1：待审关键帧"] + [
        f"图{index}：{label}" for index, (label, _) in enumerate(sheets, start=2)
    ]
    meta = asset.meta or {}
    prompt = build_review_prompt(
        shot,
        image_labels=labels,
        actual_prompt=str(meta.get("prompt_clean") or meta.get("prompt_used") or ""),
    )

    raw = await provider.generate_vision(
        prompt,
        images=images,
        system=_SYSTEM,
        json_mode=True,
        max_tokens=900,
    )
    try:
        data = prompts.extract_json(raw)
    except ValueError as exc:
        raise ValueError(f"视觉模型返回无法解析：{raw[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"视觉模型返回格式不正确：{raw[:200]}")

    review = normalize_review(
        data,
        provider=credential.provider,
        model=provider.model_for("vision"),
        media_id=int(asset.id),
        subject_count=len(_as_list(shot.subjects)),
    )
    review["reference_sheets"] = len(sheets)
    asset.meta = {**(asset.meta or {}), "visual_review": review}
    await session.flush()
    return review


async def maybe_review_keyframe(
    session: AsyncSession,
    *,
    user_id: int,
    asset: MediaAsset,
    shot: Shot | None,
) -> dict | None:
    """自动审查入口：任何失败都只记日志，不影响生图主任务。"""
    if not settings.vision_review_enabled or shot is None:
        return None
    try:
        return await review_keyframe(session, user_id=user_id, asset=asset, shot=shot)
    except Exception as exc:  # noqa: BLE001 - 审查是增强能力，绝不能让主任务失败
        logger.warning("关键帧 %s 视觉初审跳过：%s", asset.id, exc)
        return None
