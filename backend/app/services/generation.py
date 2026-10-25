"""生成编排：解析用户凭证 → 调用厂商适配器 → 结果落库 → 回写任务状态。"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.db.session import AsyncSessionLocal
from app.models.asset import ProductionAsset
from app.models.credential import ProviderCredential
from app.models.episode import Episode
from app.models.keyframe import ShotKeyframe
from app.models.media import MediaAsset
from app.models.pipeline import PipelineRun
from app.models.project import Project
from app.models.prompt_template import PromptTemplate
from app.models.shot import Shot
from app.models.task import GenerationTask
from app.services import assets as asset_service
from app.services import export as export_service
from app.services import pipeline as pipeline_service
from app.services import prompts, references as reference_service, storage
from app.services.keyframes import attach_generated_image, load_keyframes, primary_keyframe
from app.services.prompt_templates import PROP_SHEET_NEGATIVE, render_template
from app.services.providers import build_provider
from app.services.providers.base import BaseProvider, ProviderError, is_transient_error

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# 资产参考图的构图要求：角色要求中性三视图，道具要求单件正视图；
# 统一纯色背景 + 中性光线，才能作为后续分镜图生图的稳定参考。
CHARACTER_SHEET_PROMPT = (
    "角色定妆参考图：同一位角色的正面、侧面、全身三视图并排，"
    "背景为均匀的中灰色摄影棚无缝背景纸，中性均匀光线，"
    "五官与服装细节完整清晰，全身比例准确，中性站姿，画面简洁，无夸张透视"
)
# 道具参考图只写正向内容：Agnes 对「不得出现人物 / 手」这类否定句理解很差，
# 提示词里反复出现「人」反而会把人物画进画面（实测三张道具图全变成了人像）。
PROP_SHEET_PROMPT = (
    "道具标准参考图：画面主体是「{name}」这一件物品的单件产品特写，"
    "静置在画面中央，占画面 70% 以上；中灰色无缝背景纸，均匀柔和的摄影棚灯光，"
    "材质、纹理、折痕与颜色细节清晰完整，正面视角，构图简洁，无夸张透视"
)


def _appearance_lock(asset: ProductionAsset) -> str:
    """从资产档案里取「跨镜头一字不改」的外观锚点文本（五官 + 服装）。

    分镜提示词只能引用这段文字，不能引用 ``asset.prompt``：后者是定妆参考图自己的
    构图指令（三视图、灰色背景纸），混进分镜会让模型画成色卡图或者把背景纸搬进画面。
    """
    attributes = asset.attributes if isinstance(asset.attributes, dict) else {}
    parts: list[str] = []
    for key, label in (("age", "年龄"), ("appearance", "外观"), ("costume", "服装")):
        value = str(attributes.get(key) or "").strip()
        if value:
            parts.append(value if key == "appearance" else f"{label}：{value}")
    if not parts and asset.description:
        parts.append(str(asset.description).strip())
    return "；".join(parts)[:600]


async def _spawn_asset_reference_tasks(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int | None,
    assets: list[ProductionAsset],
    aspect_ratio: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    credential_id: int | None = None,
) -> list[GenerationTask]:
    """给还没有参考图的角色 / 道具各建一张参考图任务（只落库，投递由调用方决定）。

    角色出「定妆三视图」，道具出「单件正视图」，出图后由 ``_handle_image`` 自动挂成
    该资产的参考图，成为后续分镜生成的默认图像参考。没有可用图片凭证时返回空列表，
    不阻塞主流程（角色档案本身已经落库，只是暂时没有参考图）。
    """
    from app.services.dispatch import create_task

    pending = [
        asset
        for asset in assets
        if asset is not None and not asset.media_id and (asset.prompt or asset.description)
    ]
    if not pending:
        return []
    try:
        await resolve_credential(session, user_id, "image", credential_id, provider)
    except ProviderError:
        logger.info("未配置可用图片凭证，跳过资产参考图自动生成")
        return []

    tasks: list[GenerationTask] = []
    for asset in pending:
        if asset.kind == "actor":
            base = (asset.prompt or asset.description or "").strip()
            prompt = f"{base}。{CHARACTER_SHEET_PROMPT}"
            negative = "text, logo, watermark"
        else:
            # 道具档案里的描述常是「正在被推向他 / 被握在手中」这类使用状态，
            # 直接喂会被模型补上一只手甚至一个人；参考图只画物品本体，用名称重新起句。
            attributes = asset.attributes if isinstance(asset.attributes, dict) else {}
            look = str(attributes.get("appearance") or "").strip()
            base = str(asset.name or "").strip()
            if look:
                base = f"{base}，外观：{look}"
            prompt = PROP_SHEET_PROMPT.format(name=base)
            negative = PROP_SHEET_NEGATIVE
        tasks.append(
            await create_task(
                session,
                user_id=user_id,
                kind="image",
                provider=provider,
                model=model,
                project_id=project_id or asset.project_id,
                enqueue=False,
                params={
                    "asset_id": asset.id,
                    "credential_id": credential_id,
                    "prompt": prompt,
                    "negative_prompt": negative,
                    "aspect_ratio": aspect_ratio,
                    "n": 1,
                    "flow": "asset_reference",
                },
            )
        )
    return tasks


async def ensure_shot_cast_references(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int | None,
    shot: Shot,
    asset_ids: list | None = None,
    aspect_ratio: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    credential_id: int | None = None,
) -> list[GenerationTask]:
    """给本镜头出场但还没有定妆照的角色 / 道具补任务，关键帧必须排在它们之后。"""
    loaded = await _load_shot_assets(session, user_id, shot=shot, asset_ids=asset_ids)
    pending_ids = [
        int(item["id"])
        for item in loaded
        if item.get("kind") in ("actor", "prop")
        and not item.get("media_id")
        and item.get("id") is not None
    ]
    if not pending_ids:
        return []
    rows = list(
        (
            await session.execute(
                select(ProductionAsset).where(ProductionAsset.id.in_(pending_ids))
            )
        ).scalars().all()
    )
    return await _spawn_asset_reference_tasks(
        session,
        user_id=user_id,
        project_id=project_id,
        assets=rows,
        aspect_ratio=aspect_ratio,
        provider=provider,
        model=model,
        credential_id=credential_id,
    )


def _as_float(value: Any) -> float | None:
    """把厂商 meta 里的时长等字段安全转成 float（各厂商可能返回字符串）。"""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_index(value: Any, fallback: int) -> int:
    """分镜序号可能被模型写成「第1镜」，解析失败就退回出现顺序。"""
    if value in (None, ""):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        return int(digits) if digits else fallback


def _normalize_resolution(value: Any) -> str | None:
    """逐镜分辨率只接受 720p / 1080p；模型写成 1080P、1920x1080 时也收敛到同一取值。"""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "1080" in text:
        return "1080p"
    if "720" in text:
        return "720p"
    return None


# ---------------------------------------------------------------- 凭证解析
# 各能力的默认厂商优先顺序：图片 / 视频默认走 Agnes（中国站、核心模型免费且稳定），
# 文本仍默认走 DeepSeek（长文本 / JSON 结构化更稳）。用户显式指定凭证或厂商时不受此影响。
PREFERRED_PROVIDERS: dict[str, tuple[str, ...]] = {
    "text": ("deepseek", "agnes"),
    "vision": ("deepseek",),
    "image": ("agnes", "magichour"),
    "video": ("agnes", "magichour"),
}

# 新建项目弹窗两个下拉框的合法取值，必须与前端选项保持一致
VISUAL_STYLE_OPTIONS = (
    "现实", "日式动漫", "国风水墨", "油画质感", "赛博朋克", "3D 动画", "复古胶片", "卡通",
)
VIDEO_STYLE_OPTIONS = (
    "真人都市", "古装仙侠", "现代校园", "悬疑惊悚", "甜宠爱情", "科幻未来", "历史正剧", "武侠江湖",
)


def _preference_rank(capability: str, provider: str) -> int:
    """返回该厂商在能力优先顺序中的名次；不在列表里的排在最后。"""
    order = PREFERRED_PROVIDERS.get(capability, ())
    return order.index(provider) if provider in order else len(order)


async def resolve_credential(
    session: AsyncSession,
    user_id: int,
    capability: str,
    credential_id: int | None = None,
    provider_key: str | None = None,
) -> ProviderCredential:
    stmt = select(ProviderCredential).where(ProviderCredential.user_id == user_id)
    if credential_id:
        stmt = stmt.where(ProviderCredential.id == credential_id)
    elif provider_key:
        stmt = stmt.where(ProviderCredential.provider == provider_key)

    rows = list((await session.execute(stmt)).scalars().all())
    matched = [row for row in rows if capability in row.capability_set()]
    if not matched:
        raise ProviderError(
            f"未找到可用于「{capability}」生成的 API Key，请先到「密钥管理」中添加并勾选对应能力"
        )
    matched.sort(key=lambda row: (_preference_rank(capability, row.provider), not row.is_default, row.id))
    return matched[0]


async def resolve_fallback_chain(
    session: AsyncSession,
    user_id: int,
    capability: str,
    credential_id: int | None = None,
    provider_key: str | None = None,
) -> list[ProviderCredential]:
    """按「候选兜底链」顺序返回该用户具备该能力的全部凭证。

    - 显式指定 ``credential_id`` 时把它排到第一位，其余按 ``(not is_default, id)`` 排序
      （与 ``resolve_credential`` 口径一致）；
    - 否则把指定 ``provider_key`` 的凭证排在前面，其余仍按 ``(not is_default, id)`` 排序，
      以便主厂商临时故障时能自动回退到其它厂商的凭证。
    - 全部候选为空时抛出与 ``resolve_credential`` 同款提示。
    """
    stmt = select(ProviderCredential).where(ProviderCredential.user_id == user_id)
    rows = list((await session.execute(stmt)).scalars().all())
    matched = [row for row in rows if capability in row.capability_set()]
    if not matched:
        raise ProviderError(
            f"未找到可用于「{capability}」生成的 API Key，请先到「密钥管理」中添加并勾选对应能力"
        )
    if credential_id:
        matched.sort(key=lambda row: (row.id != credential_id, _preference_rank(capability, row.provider), not row.is_default, row.id))
    else:
        matched.sort(
            key=lambda row: (
                bool(provider_key) and row.provider != provider_key,
                _preference_rank(capability, row.provider),
                not row.is_default,
                row.id,
            )
        )
    return matched


def build_provider_from(credential: ProviderCredential) -> BaseProvider:
    return build_provider(
        credential.provider,
        decrypt_secret(credential.api_key_encrypted),
        credential.base_url,
    )


# ---------------------------------------------------------------- 进度回写
async def _write_progress(task_id: int, value: int) -> None:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(GenerationTask)
                .where(GenerationTask.id == task_id)
                .values(progress=max(0, min(100, int(value))))
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - 进度写失败不应影响主流程
        pass


def progress_callback(task_id: int) -> Callable[[int], None]:
    def callback(value: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_write_progress(task_id, value))

    return callback


# ---------------------------------------------------------------- 辅助
def _project_style_text(project: Project | None) -> str | None:
    """项目级风格文案：画法（视觉风格）与色调质感（整体风格关键词）都要进提示词。

    以前是 ``visual_style or style``——选了视觉风格后，那串「整体风格关键词」就被顶掉了。
    """
    if not project:
        return None
    parts = [
        str(item).strip() for item in (project.visual_style, project.style) if str(item or "").strip()
    ]
    return "，".join(dict.fromkeys(parts)) or None


async def _project_of_shot(session: AsyncSession, shot: Shot | None) -> Project | None:
    if not shot:
        return None
    episode = await session.get(Episode, shot.episode_id)
    if not episode:
        return None
    return await session.get(Project, episode.project_id)


async def _aspect_of(session: AsyncSession, shot: Shot | None) -> str:
    project = await _project_of_shot(session, shot)
    return project.aspect_ratio if project else "9:16"


async def _style_of(session: AsyncSession, shot: Shot | None) -> str | None:
    project = await _project_of_shot(session, shot)
    return project.style if project else None


# ---------------------------------------------------------------- 前后镜头上下文
async def load_shot_neighbors(
    session: AsyncSession, shot: Shot
) -> tuple[Shot | None, Shot | None]:
    """取同一分集内、按 order_index 紧邻的上一个 / 下一个分镜。

    用于把「前后镜头上下文」注入生成提示词，保障单独生成 / 重绘某个镜头时
    画面、人物与色调仍与整段戏保持连续。
    """
    prev_shot = (
        await session.execute(
            select(Shot)
            .where(Shot.episode_id == shot.episode_id, Shot.order_index < shot.order_index)
            .order_by(Shot.order_index.desc())
            .limit(1)
        )
    ).scalars().first()
    next_shot = (
        await session.execute(
            select(Shot)
            .where(Shot.episode_id == shot.episode_id, Shot.order_index > shot.order_index)
            .order_by(Shot.order_index.asc())
            .limit(1)
        )
    ).scalars().first()
    return prev_shot, next_shot


def _neighbor_summary(shot: Shot | None, *, field: str) -> str | None:
    """取相邻镜头最能代表其画面的文本：优先该字段的提示词，其次画面描述 / 标题。"""
    if not shot:
        return None
    return (getattr(shot, field, None) or shot.description or shot.title or "").strip() or None


def _neighbor_brief(shot: Shot | None, *, field: str, which: str) -> str | None:
    """相邻镜头用于衔接的一句话摘要。

    优先用首尾状态（人物位置、神态、道具），它只描述「承接那一刻」；
    没有状态时才退到相邻提示词的开头一小段。整段相邻提示词会把别的镜头的
    构图、站位、动作一起塞进来，与本镜头互相冲突，画面反而被拉稀。
    """
    if not shot:
        return None
    state_text = _state_summary(shot, which=which)
    if state_text:
        return state_text
    text = _neighbor_summary(shot, field=field)
    if not text:
        return None
    return text[:120].rstrip("，,；;。 ") + "…" if len(text) > 120 else text


async def _continuity_block(session: AsyncSession, shot: Shot, *, field: str) -> str | None:
    """取相邻镜头上下文块；没有相邻镜头时返回 None。"""
    prev_shot, next_shot = await load_shot_neighbors(session, shot)
    block = prompts.build_continuity_block(
        prev_summary=_neighbor_brief(prev_shot, field=field, which="end"),
        next_summary=_neighbor_brief(next_shot, field=field, which="start"),
    )
    return block or None


# ---------------------------------------------------------------- 资产 / 模板
async def _load_assets(
    session: AsyncSession, user_id: int, asset_ids: list | None
) -> list[dict]:
    """按 id 取出该用户的资产，转成生成用的 dict。

    除外观描述（拼进提示词）外还带 ``reference_url``：资产参考图的**公网可访问地址**。
    它会被当作「图像参考」送进模型做图生图，这是让同一角色在不同镜头里长得一样的
    关键手段——只靠文字描述无法锁定五官。

    公网地址取法：优先厂商 CDN 原址（生成图落库时记在 meta.source_url），
    其次按 PUBLIC_MEDIA_BASE_URL 把本地地址转成公网地址；都拿不到就置空
    （厂商拉不到图会让整单失败，宁可不传）。
    """
    ids = [int(item) for item in (asset_ids or []) if item is not None]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(ProductionAsset).where(
                ProductionAsset.user_id == user_id, ProductionAsset.id.in_(ids)
            )
        )
    ).scalars().all()

    media_ids = {item.media_id for item in rows if item.media_id}
    urls: dict[int, str] = {}
    if media_ids:
        media_rows = (
            await session.execute(
                select(MediaAsset.id, MediaAsset.url, MediaAsset.meta).where(
                    MediaAsset.id.in_(media_ids)
                )
            )
        ).all()
        for media_id, url, meta in media_rows:
            public = (meta or {}).get("source_url") or storage.to_public_url(url)
            if public and str(public).startswith(("http://", "https://")):
                urls[media_id] = str(public)

    return [
        {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "prompt": item.prompt,
            "description": item.description,
            "appearance": _appearance_lock(item),
            "media_id": item.media_id,
            "reference_url": urls.get(item.media_id) if item.media_id else None,
            # 参考图已生成但拿不到公网地址（本地部署没配 PUBLIC_MEDIA_BASE_URL）：
            # 送不进模型，需要给用户明确提示，而不是静默当没有。
            "reference_missing": bool(item.media_id) and item.media_id not in urls,
        }
        for item in rows
    ]


async def _load_shot_assets(
    session: AsyncSession,
    user_id: int,
    *,
    shot: Shot | None,
    asset_ids: list | None,
) -> list[dict]:
    """显式关联的资产 + 镜头里各主体 / 道具绑定的资产，一起去重加载。

    分镜在拆解阶段就把角色 / 道具的 asset_id 写进 subjects / props，
    生成时无需用户再手动勾选，也不会漏掉「本镜头出场但没勾选」的角色。
    """
    ids: list[int] = []
    for value in asset_ids or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if shot is not None:
        for group in (shot.subjects or [], shot.props or []):
            for item in group:
                if isinstance(item, dict) and item.get("asset_id") is not None:
                    try:
                        ids.append(int(item["asset_id"]))
                    except (TypeError, ValueError):
                        continue

        # 旧分镜 / 手工分镜可能只有姓名没有 asset_id：按项目内同名资产兜底解析，
        # 同时按 start_state.location 找匹配的场景资产。
        # 共享演员（is_global / project_id 为空）也要参与匹配，否则跨项目复用的角色
        # 在分镜里只有姓名时拿不到定妆照，关键帧就会退化成纯文生图。
        episode = await session.get(Episode, shot.episode_id)
        project_id = episode.project_id if episode else None
        if project_id:
            rows = (
                await session.execute(
                    select(ProductionAsset).where(
                        ProductionAsset.user_id == user_id,
                        or_(
                            ProductionAsset.project_id == project_id,
                            ProductionAsset.is_global.is_(True),
                        ),
                    )
                )
            ).scalars().all()
            actors = [item for item in rows if item.kind == "actor"]
            props = [item for item in rows if item.kind == "prop"]
            scenes = [item for item in rows if item.kind == "scene"]
            for subject in shot.subjects or []:
                if not isinstance(subject, dict) or subject.get("asset_id"):
                    continue
                match = next(
                    (item for item in actors if _same_name(item.name, subject.get("name"))),
                    None,
                )
                if match:
                    ids.append(match.id)
            for prop in shot.props or []:
                if not isinstance(prop, dict) or prop.get("asset_id"):
                    continue
                match = next(
                    (item for item in props if _same_name(item.name, prop.get("name"))),
                    None,
                )
                if match:
                    ids.append(match.id)
            location = (
                shot.start_state.get("location")
                if isinstance(shot.start_state, dict)
                else None
            )
            if location:
                for scene in scenes:
                    if _same_name(scene.name, location):
                        ids.append(scene.id)

    deduped = list(dict.fromkeys(ids))
    return await _load_assets(session, user_id, deduped)


def _same_name(left: object, right: object) -> bool:
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _pick_references(
    assets: list[dict],
    *,
    shot: Shot | None,
    explicit_ids: list | None = None,
    limit: int = 4,
) -> list[dict]:
    """按本镜头实际出场的主体挑选参考图，并生成「图1/图2」图例。

    - 用户显式勾选的资产优先，视为手动指定；
    - 有 subjects 时只送 subjects 里点名的角色，避免把没出场的角色也塞给模型，
      导致「伞换人拿 / 脸串角色」；
    - 没有结构化主体（旧分镜）时才回退到全部角色资产；
    - 道具、场景参考图排在角色之后，总数限制在 4 张以内。
    """
    if not assets:
        return []
    subjects = [item for item in (shot.subjects or []) if isinstance(item, dict)] if shot else []
    props = [item for item in (shot.props or []) if isinstance(item, dict)] if shot else []
    explicit: set[int] = set()
    for value in explicit_ids or []:
        try:
            explicit.add(int(value))
        except (TypeError, ValueError):
            continue

    references: list[dict] = []
    used_urls: set[str] = set()

    def add(asset: dict, purpose: str) -> None:
        url = str(asset.get("reference_url") or "").strip()
        if not url or url in used_urls or len(references) >= limit:
            return
        used_urls.add(url)
        references.append(
            {
                "kind": asset.get("kind"),
                "name": asset.get("name"),
                "purpose": purpose,
                "url": url,
                "group": _is_group_asset(asset),
            }
        )

    for item in assets:
        if item.get("id") in explicit:
            add(item, "用户显式指定的参考图，严格遵循其外观")

    actors = [item for item in assets if item.get("kind") == "actor"]
    if subjects:
        for subject in subjects:
            match = next(
                (item for item in actors if _same_name(item.get("name"), subject.get("name"))),
                None,
            )
            if match:
                add(match, "本镜头出场角色，必须沿用此人的脸型、发型与服装")
    else:
        for item in actors:
            add(item, "角色定妆参考")

    for prop in props:
        match = next(
            (
                item
                for item in assets
                if item.get("kind") == "prop" and _same_name(item.get("name"), prop.get("name"))
            ),
            None,
        )
        if match:
            add(match, "本镜头道具的外观与状态")

    for item in assets:
        if item.get("kind") == "scene":
            add(item, "本镜头场景环境")

    return references


def _shot_prompt_context(shot: Shot | None) -> dict[str, Any]:
    """生成提示词要用的结构化镜头事实（旧分镜没有这些字段时全为空）。"""
    if shot is None:
        return {}
    return {
        "subjects": shot.subjects or None,
        "props": shot.props or None,
        "timeline": shot.action_timeline or None,
        "start_state": shot.start_state,
        "end_state": shot.end_state,
        "directives": shot.directives,
        "duration": shot.duration_seconds,
    }


async def _media_reference(
    session: AsyncSession,
    media_id: int | None,
    *,
    name: str,
    purpose: str,
    kind: str = "continuity",
) -> dict | None:
    """把一张已生成的图片转成可送进模型的连续性参考；拿不到公网地址则返回 None。"""
    if not media_id:
        return None
    media = await session.get(MediaAsset, int(media_id))
    if media is None or media.kind != "image":
        return None
    url = (media.meta or {}).get("source_url") or storage.to_public_url(media.url)
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    return {"kind": kind, "name": name, "purpose": purpose, "url": str(url)}


def _named_subject_set(shot: Shot) -> set[str]:
    return {
        str(item.get("name") or "").strip()
        for item in (shot.subjects or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }


# 群像资产的参考图里本来就有多个人；把它当普通角色送进 i2i，模型会照着参考图复制人数
_GROUP_ASSET_HINTS = ("群像", "群体", "多名", "数名", "一群人", "众人", "全班", "学生们", "同学们")


def _is_group_asset(asset: dict) -> bool:
    text = "".join(str(asset.get(key) or "") for key in ("name", "prompt", "appearance"))
    return any(hint in text for hint in _GROUP_ASSET_HINTS)


# 同场景不同机位常写成「教室后门 / 教室过道 / 教室窗边」，比较场景时先剥掉机位后缀
_SCENE_SUFFIXES = ("后门", "前门", "门口", "过道", "走廊", "窗边", "讲台", "座位", "前排", "后排")
# 这些是场景构件而不是可拿在手里的道具；空镜里拿它的产品图当参考会把现场画成摄影棚
_SCENE_ELEMENT_HINTS = (
    "门", "窗", "墙", "黑板", "讲台", "走廊", "教室", "玻璃", "地板", "天花板", "课桌", "桌椅",
)


def _scene_key(shot: Shot) -> str:
    location = str((shot.start_state or {}).get("location") or "").strip()
    for suffix in _SCENE_SUFFIXES:
        if location.endswith(suffix):
            location = location[: -len(suffix)]
    return location.strip()


def _is_scene_element(name: str | None) -> bool:
    return any(hint in str(name or "") for hint in _SCENE_ELEMENT_HINTS)


_POSTURE_PATTERNS = (
    ("躺", ("躺", "卧")),
    ("坐", ("坐",)),
    ("站", ("站", "立")),
    ("蹲", ("蹲",)),
    ("跪", ("跪",)),
    ("走", ("迈", "行走", "跑", "奔")),
)


def _posture_of(*texts: str) -> str | None:
    joined = "".join(texts)
    for label, words in _POSTURE_PATTERNS:
        if any(word in joined for word in words):
            return label
    return None


_HIDDEN_FACE_HINTS = ("背对镜头", "背对", "未露脸", "后脑勺", "背影")


def _hides_face(shot: Shot) -> bool:
    """该镜头的主体是否看不到脸（背影 / 后脑勺）。"""
    joined = "".join(
        str(item.get(key) or "")
        for item in (shot.subjects or [])
        if isinstance(item, dict)
        for key in ("position", "expression", "gaze")
    )
    return any(hint in joined for hint in _HIDDEN_FACE_HINTS)


def _staging_conflicts(prev_shot: Shot, shot: Shot) -> bool:
    """同一个人上一镜坐着、本镜头要站着时，不能拿上一帧当参考。

    实测：Agnes 的图生图会把参考帧当成底图，把坐着的人留在原位，再按描述补一个站着的
    同一个人，于是两人镜头画成三人。站位一致时才允许用上一帧接力。
    """
    previous = {
        str(item.get("name") or "").strip(): (
            str(item.get("position") or ""), str(item.get("action") or "")
        )
        for item in (prev_shot.subjects or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    for item in shot.subjects or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name not in previous:
            continue
        before = _posture_of(*previous[name])
        after = _posture_of(str(item.get("position") or ""), str(item.get("action") or ""))
        if before and after and before != after:
            return True
    return False


async def _previous_shot_reference(session: AsyncSession, shot: Shot) -> dict | None:
    """同一场戏里、演员集合相同的最近一张已出图关键帧。

    向前跳过空镜与其他角色的镜头：97(两人) → 98(空镜) → 99(两人) 时，
    99 应该接 97 的同一组人，而不是因为紧邻的 98 是空镜就退化成纯文生图。
    空镜自身没有演员集合，不拿别人的帧当参考，避免把人物复制进门/走廊。
    """
    current_cast = _named_subject_set(shot)
    if not current_cast:
        return None
    target_scene = _scene_key(shot)
    previous_shots = (
        await session.execute(
            select(Shot)
            .where(Shot.episode_id == shot.episode_id, Shot.order_index < shot.order_index)
            .order_by(Shot.order_index.desc())
        )
    ).scalars().all()
    for prev_shot in previous_shots:
        if _named_subject_set(prev_shot) != current_cast:
            continue
        if _scene_key(prev_shot) != target_scene:
            continue
        # 上一帧露脸、本镜要背对镜头时，i2i 会保留上一帧那张脸再补一个背影，画出两个人
        if _hides_face(shot) and not _hides_face(prev_shot):
            continue
        if _staging_conflicts(prev_shot, shot):
            continue
        keyframes = (await load_keyframes(session, [prev_shot.id])).get(prev_shot.id, [])
        primary = primary_keyframe(keyframes)
        if not primary or not primary.media_id:
            continue
        pose_notes = "；".join(
            f"「{str(item.get('name') or '').strip()}」{str(item.get('position') or item.get('action') or '').strip()}"
            for item in (prev_shot.subjects or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        )
        return await _media_reference(
            session,
            primary.media_id,
            name=f"第 {prev_shot.order_index} 镜头的关键帧",
            purpose=(
                "上一镜头的关键帧，里面的人就是本镜头 subjects 的同一批人"
                + (f"（参考帧里的姿态：{pose_notes}）" if pose_notes else "")
                + "。这张图只用于锁定五官、脸型、体型、发型、服装、场景与光线；"
                "本镜头里这些人的位置与动作按 subjects 描述已经改变，"
                "严禁沿用参考帧的站位与构图，"
                "更严禁把参考帧里的某个人保留在原位、又按本镜头描述再画一个同一个人"
            ),
        )
    return None


async def _next_continuation_frame(session: AsyncSession, shot: Shot) -> str | None:
    """下一镜的首帧公网地址；仅同场景同组人物时作为本段视频的尾帧。

    首尾帧相接适合「同一场戏连续动作拆成两段」，硬切镜头不适用——
    所以换场、换人、下一帧拿不到公网地址时一律不传尾帧。
    """
    _, next_shot = await load_shot_neighbors(session, shot)
    if not next_shot:
        return None
    current_cast = _named_subject_set(shot)
    if not current_cast or _named_subject_set(next_shot) != current_cast:
        return None
    if _scene_key(next_shot) != _scene_key(shot):
        return None
    keyframes = (await load_keyframes(session, [next_shot.id])).get(next_shot.id, [])
    primary = primary_keyframe(keyframes)
    if not primary or not primary.media_id:
        return None
    media = await session.get(MediaAsset, primary.media_id)
    if not media:
        return None
    public = (media.meta or {}).get("source_url") or storage.to_public_url(media.url)
    return str(public) if public and str(public).startswith(("http://", "https://")) else None


def _state_summary(shot: Shot | None, *, which: str) -> str | None:
    """把首/尾状态压成一行，供相邻镜头衔接使用（比整段提示词更聚焦、也更省字数）。"""
    if not shot:
        return None
    state = shot.start_state if which == "start" else shot.end_state
    if not isinstance(state, dict):
        return None
    labels = (
        ("location", "地点"),
        ("time_of_day", "时间"),
        ("weather", "天气"),
        ("light", "光线"),
        ("positions", "人物位置/朝向"),
        ("expression", "神态"),
        ("props", "道具"),
    )
    parts = [f"{label}：{state[key]}" for key, label in labels if state.get(key)]
    return "；".join(parts) or None


_TIMED_BEAT_RE = re.compile(r"\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*s\s*[:：]", re.IGNORECASE)


def _strip_timed_beats(text: str) -> str:
    """分镜自带的秒级动作交给时间轴块，base 只留场景 / 光线等非时序描述，避免同一动作占两份预算。"""
    if not text or not _TIMED_BEAT_RE.search(text):
        return text
    sentences = re.split(r"(?<=[。！？])", text)
    kept = [part for part in sentences if part.strip() and not _TIMED_BEAT_RE.search(part)]
    result = "".join(kept).strip()
    return result or text


async def _video_continuity_block(session: AsyncSession, shot: Shot) -> str | None:
    """视频衔接用首尾状态而不是整段相邻提示词：避免把下一镜的动作/场景提前演出来。

    旧分镜没有首尾状态时退到相邻提示词前 120 字，至少保住人物与场景，不要整段丢掉衔接。
    """
    prev_shot, next_shot = await load_shot_neighbors(session, shot)
    block = prompts.build_continuity_block(
        prev_summary=_neighbor_brief(prev_shot, field="video_prompt", which="end"),
        next_summary=_neighbor_brief(next_shot, field="video_prompt", which="start"),
    )
    return block or None


async def _render_template_prompt(
    session: AsyncSession,
    user_id: int,
    template_id: int,
    variables: dict,
    fallback: str,
) -> str:
    """按模板渲染出主体提示词；模板不存在 / 不可见时回落到 fallback。"""
    template = await session.get(PromptTemplate, int(template_id))
    if not template or not (template.is_system or template.user_id == user_id):
        return fallback
    rendered = render_template(template.content, variables).strip()
    return rendered or fallback


async def _base_prompt_with_template(
    session: AsyncSession,
    *,
    task: GenerationTask,
    shot: Shot | None,
    params: dict,
    aspect_ratio: str,
    project: Project | None,
    fallback: str,
) -> str:
    """若请求带了 template_id，则用模板渲染后的内容作为主体提示词；否则用 fallback。"""
    template_id = params.get("template_id")
    if not template_id:
        return fallback
    variables = {
        "description": fallback
        or (shot.description if shot else "")
        or (shot.title if shot else "")
        or "",
        "extra_details": "",
        "visual_style": (project.visual_style if project else "") or "",
        "style": (project.style if project else "") or "",
        "aspect_ratio": aspect_ratio,
        "base_negative": prompts.DEFAULT_NEGATIVE,
    }
    variables.update(params.get("template_variables") or {})
    return await _render_template_prompt(session, task.user_id, int(template_id), variables, fallback)


# ---------------------------------------------------------------- 各类型处理
async def _generate_json(
    provider: BaseProvider,
    prompt: str,
    *,
    system: str,
    model: str | None,
    temperature: float,
    # 推理型模型（DeepSeek V4）会先消耗大量思考 token，再输出 JSON，
    # 8192 对「长剧本拆解」不够：实测思考 3.5k + 正文 8k，容易 finish_reason=length。
    max_tokens: int = 16384,
    attempts: int = 2,
) -> dict[str, Any]:
    """要求模型输出 JSON 并解析；偶发的空返回 / 格式瑕疵自动重试。"""
    last_error: Exception | None = None
    for _ in range(attempts):
        raw = await provider.generate_text(
            prompt,
            system=system,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        try:
            return prompts.extract_json(raw)
        except ValueError as exc:
            last_error = exc
    raise ProviderError(f"模型返回内容无法解析为 JSON：{last_error}")


async def suggest_project_setting(
    session: AsyncSession,
    *,
    user_id: int,
    idea: str,
    aspect_ratio: str,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """新建项目时的 AI 智能补全：粗主题 → 标题 / 简介 / 风格 / 标签。

    同步返回，不创建异步任务（用于新建项目弹窗的即时填充）。
    """
    credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    provider = build_provider_from(credential)
    raw = await _generate_json(
        provider,
        prompts.build_project_suggest_prompt(idea=idea, aspect_ratio=aspect_ratio),
        system=prompts.PROJECT_SUGGEST_SYSTEM,
        model=model,
        temperature=0.7,
        max_tokens=2048,
    )
    allowed = ("9:16", "16:9", "1:1", "4:3")
    suggested_ratio = str(raw.get("aspect_ratio") or "").strip()
    visual_style = str(raw.get("visual_style") or "").strip()
    video_style = str(raw.get("video_style") or "").strip()
    return {
        "title": str(raw.get("title") or "").strip()[:200] or idea.strip()[:20],
        "description": str(raw.get("description") or "").strip()[:500],
        # 下拉框只接受白名单里的值，模型自造的写法会让选项显示为空
        "visual_style": visual_style if visual_style in VISUAL_STYLE_OPTIONS else "",
        "video_style": video_style if video_style in VIDEO_STYLE_OPTIONS else "",
        "style": str(raw.get("style") or "").strip()[:200],
        "aspect_ratio": suggested_ratio if suggested_ratio in allowed else aspect_ratio,
        "tags": [str(tag).strip() for tag in (raw.get("tags") or []) if str(tag).strip()][:5],
    }


def _clamp_int(value: Any, *, minimum: int, maximum: int, fallback: int) -> int:
    """模型给的数量类字段可能是字符串 / 越界，统一收到合法区间。"""
    if value in (None, ""):
        return fallback
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _rescale_beats(beats: list[dict], planned: int, total: int) -> list[dict]:
    """把各段镜头数等比缩放到用户指定的总数，保证段落之和与实际出镜数一致。"""
    scaled: list[dict] = []
    assigned = 0
    for index, beat in enumerate(beats):
        if index == len(beats) - 1:
            shots = max(1, total - assigned)
        else:
            shots = max(1, round(int(beat.get("shots") or 0) * total / planned))
        assigned += shots
        scaled.append({**beat, "shots": shots})
    return scaled


async def suggest_shot_plan(
    session: AsyncSession,
    *,
    user_id: int,
    episode_id: int,
    shot_count: int | None = None,
    target_seconds: int | None = None,
    style: str | None = None,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """拆解前先给规划建议：只回答「拆几镜、每镜多长、为什么」，不动分镜、不删旧镜头。

    同步返回，供弹窗即时展示；用户确认后再带着这份方案发起真正的拆解任务。
    """
    episode = await session.get(Episode, int(episode_id))
    if not episode:
        raise ProviderError("分集不存在")
    project = await session.get(Project, episode.project_id)
    script = (episode.script_text or "").strip()
    if not script:
        raise ProviderError("分集剧本为空，请先填写剧本")

    credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    provider = build_provider_from(credential)
    raw = await _generate_json(
        provider,
        prompts.build_shot_plan_prompt(
            title=episode.title,
            script=script,
            style=style or (project.style if project else None),
            aspect_ratio=project.aspect_ratio if project else "9:16",
            shot_count=shot_count,
            target_seconds=target_seconds,
        ),
        system=prompts.SHOT_PLAN_SYSTEM,
        model=model,
        temperature=0.5,
        max_tokens=2048,
    )
    plan = prompts.normalize_plan(raw) or {"reasoning": "", "beats": []}
    beats = plan["beats"]
    planned = sum(int(beat.get("shots") or 0) for beat in beats)

    count = _clamp_int(shot_count or raw.get("shot_count") or planned, minimum=1, maximum=60, fallback=8)
    if planned and planned != count:
        beats = _rescale_beats(beats, planned, count)
    seconds = _clamp_int(raw.get("shot_seconds"), minimum=5, maximum=12, fallback=8)
    target = _clamp_int(
        target_seconds or raw.get("target_seconds") or count * seconds,
        minimum=5,
        maximum=900,
        fallback=count * seconds,
    )
    return {
        "reasoning": plan["reasoning"],
        "shot_count": count,
        "shot_seconds": seconds,
        "target_seconds": target,
        "beats": beats,
    }




def _field_current_value(shot: Shot, field: str) -> str | None:
    """取值：镜头语言分层字段存在 directives 里，其余是 shot 自己的列。"""
    if field in prompts.DIRECTIVE_FIELD_KEYS:
        value = (shot.directives or {}).get(field)
        if isinstance(value, list):
            return "、".join(str(item) for item in value) or None
        return str(value) if value else None
    return getattr(shot, field, None)


async def suggest_shot_field(
    session: AsyncSession,
    *,
    user_id: int,
    shot: Shot,
    field: str,
    hint: str | None = None,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
) -> str:
    """为分镜的单个字段生成内容（画面描述 / 关键帧提示词 / 视频提示词）。

    同步返回，不创建异步任务（用于分镜详情页的「AI 生成」按钮即时填充）。
    会自动带入项目风格、画幅与前后镜头上下文，保证与整段戏连贯。
    """
    if field not in prompts.SHOT_FIELD_KEYS:
        raise ProviderError(f"不支持的字段：{field}")
    credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    provider = build_provider_from(credential)
    prev_shot, next_shot = await load_shot_neighbors(session, shot)
    neighbor_field = "video_prompt" if field == "video_prompt" else "image_prompt"
    project = await _project_of_shot(session, shot)
    episode = await session.get(Episode, shot.episode_id)
    characters = (
        await asset_service.load_project_characters(
            session, user_id=user_id, project_id=project.id
        )
        if project
        else []
    )
    assets = await _load_shot_assets(session, user_id, shot=shot, asset_ids=None)
    raw = await _generate_json(
        provider,
        prompts.build_shot_field_prompt(
            field=field,
            title=shot.title,
            description=shot.description,
            dialogue=shot.dialogue,
            style=await _style_of(session, shot),
            aspect_ratio=await _aspect_of(session, shot),
            prev_summary=_neighbor_summary(prev_shot, field=neighbor_field),
            next_summary=_neighbor_summary(next_shot, field=neighbor_field),
            hint=hint,
            current_value=_field_current_value(shot, field),
            script=episode.script_text if episode else None,
            characters=characters,
            assets=assets,
            subjects=shot.subjects or None,
            props=shot.props or None,
            timeline=shot.action_timeline or None,
            start_state=shot.start_state,
            end_state=shot.end_state,
            directives=shot.directives,
            duration=shot.duration_seconds,
        ),
        system=prompts.SHOT_FIELD_SYSTEM,
        model=model,
        temperature=0.9,
        max_tokens=2048,
    )
    value = str(raw.get("value") or "").strip()
    if not value:
        raise ProviderError("模型未返回内容，请重试或更换模型")
    return value


async def build_prompt_preview(
    session: AsyncSession,
    *,
    user_id: int,
    shot: Shot,
    kind: str = "image",
    extra_prompt: str | None = None,
) -> dict[str, Any]:
    """编译预览：按当前分层字段拼出真实会发送的提示词，并给出分块字数。

    与生成时共用 ``compose_generation_prompt``，所以「哪块会被裁」在点生成之前就能看到。
    参考图图例按本镜主体可解析到的资产估算，实际选图仍以生成时为准。
    """
    project = await _project_of_shot(session, shot)
    aspect_ratio = await _aspect_of(session, shot)
    style = await _style_of(session, shot)
    assets = await _load_shot_assets(session, user_id, shot=shot, asset_ids=None)

    if kind == "video":
        base = (extra_prompt or shot.video_prompt or shot.image_prompt or shot.description or "").strip()
        # 与真实生成一致：自动提示词里的秒级拍点交给时间轴块，避免预览多算一份
        if not extra_prompt and shot.action_timeline:
            base = _strip_timed_beats(base)
    else:
        base = (extra_prompt or shot.image_prompt or shot.description or "").strip()

    references = [
        {"kind": str(item.get("kind") or "actor"), "name": str(item.get("name") or ""), "url": None}
        for item in assets
        if item.get("name")
    ][:4]
    # 相邻镜头衔接块会真实参与编译，预览必须一起算，否则总字数会少算一截
    continuity = (
        await _video_continuity_block(session, shot)
        if kind == "video"
        else await _continuity_block(session, shot, field="image_prompt")
    )
    max_chars = (
        prompts.MAX_VIDEO_PROMPT_CHARS
        if kind == "video"
        else prompts.MAX_IMAGE_PROMPT_CHARS
    )
    composed, blocks = prompts.compose_generation_prompt(
        base=base,
        kind=kind,
        style=style,
        aspect_ratio=aspect_ratio,
        assets=assets or None,
        negative=None,
        continuity=continuity,
        subjects=shot.subjects or None,
        props=shot.props or None,
        timeline=shot.action_timeline or None,
        duration=shot.duration_seconds,
        start_state=shot.start_state,
        end_state=shot.end_state,
        directives=shot.directives,
        references=references or None,
        max_chars=max_chars,
        empty_shot=not (shot.subjects or []),
        with_breakdown=True,
    )
    assert isinstance(composed, str)
    dropped = [item["tag"] for item in blocks if item["chars"] == 0]
    return {
        "kind": kind,
        "prompt": composed,
        "chars": len(composed),
        "max_chars": max_chars,
        "blocks": [
            {
                "tag": item["tag"],
                "label": _BLOCK_LABELS.get(item["tag"], item["tag"]),
                "chars": item["chars"],
            }
            for item in blocks
        ],
        "dropped": dropped,
        "aspect_ratio": aspect_ratio,
        "project_title": project.title if project else None,
    }


# 预览里各分块的中文名，便于用户直接看出哪一层在吃预算
_BLOCK_LABELS = {
    "base": "正文（动作描述）",
    "style": "风格收束",
    "subjects": "主体与道具绑定",
    "legend": "参考图图例",
    "state": "场景与首尾状态",
    "directives": "镜头语言",
    "timeline": "动作时间轴",
    "extra": "本张补充要求",
    "assets": "资产外观",
    "quality": "画质与运动要求",
    "continuity": "前后镜头衔接",
    "count_check": "人物数量校验",
    "negative": "负面提示",
}


async def _handle_text(session: AsyncSession, task: GenerationTask) -> dict[str, Any]:
    params = task.params or {}
    credential = await resolve_credential(
        session, task.user_id, "text", params.get("credential_id"), params.get("provider") or task.provider
    )
    provider = build_provider_from(credential)
    model = params.get("model") or task.model

    action = params.get("action") or "custom"

    if action == "forge":
        # 主题 / 世界观 → 剧本 → 分镜指令（两步串联，一次任务完成）
        project = await session.get(Project, task.project_id) if task.project_id else None
        if not project:
            raise ProviderError("项目不存在，无法生成剧本")
        premise = (params.get("premise") or "").strip()
        if not premise:
            raise ProviderError("请先填写主题 / 世界观")

        next_index = int(
            (
                await session.execute(
                    select(func.coalesce(func.max(Episode.order_index), 0)).where(
                        Episode.project_id == project.id
                    )
                )
            ).scalar_one()
        ) + 1

        screenplay_prompt = prompts.build_screenplay_prompt(
            premise=premise,
            style=params.get("style") or project.style,
            aspect_ratio=project.aspect_ratio,
            episode_index=next_index,
            target_seconds=int(params.get("target_seconds") or 60),
            extra=params.get("extra"),
        )
        raw = await _generate_json(
            provider,
            screenplay_prompt,
            system=prompts.SCREENPLAY_SYSTEM,
            model=model,
            temperature=0.85,
        )
        screenplay = raw
        script = (screenplay.get("script") or "").strip()
        if not script:
            raise ProviderError("模型未返回剧本正文，请重试或更换模型")
        characters = screenplay.get("characters") or []

        # 角色档案：从剧本提炼稳定外观与固定服装，落库为演员资产。
        # 档案抽取失败时降级用剧本里的粗略人物信息，不能让整个 forge 白跑。
        profiles: list[dict] = []
        try:
            profile_raw = await _generate_json(
                provider,
                prompts.build_character_profile_prompt(
                    script=script,
                    style=params.get("style") or project.style,
                    aspect_ratio=project.aspect_ratio,
                ),
                system=prompts.CHARACTER_PROFILE_SYSTEM,
                model=model,
                temperature=0.6,
                max_tokens=4096,
            )
            profiles = prompts.parse_character_profiles(profile_raw)
        except ProviderError as exc:
            logger.warning("角色档案抽取失败，降级使用剧本人物信息：%s", exc)
        if not profiles:
            profiles = [
                {
                    "name": str(item.get("name") or "").strip(),
                    "age": item.get("age") or "",
                    "role": item.get("role") or "",
                    "appearance": "",
                    "costume": "",
                    "traits": item.get("traits") or "",
                    "tags": [],
                }
                for item in characters
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
        characters = await asset_service.ensure_character_assets(
            session,
            user_id=task.user_id,
            project_id=project.id,
            profiles=profiles,
        )

        episode = Episode(
            project_id=project.id,
            order_index=next_index,
            title=(screenplay.get("title") or f"第 {next_index} 集").strip()[:200],
            synopsis=screenplay.get("synopsis"),
            script_text=script,
            status="scripted",
        )
        session.add(episode)
        await session.flush()

        breakdown_prompt = prompts.build_breakdown_prompt(
            title=episode.title,
            script=script,
            style=params.get("style") or project.style,
            aspect_ratio=project.aspect_ratio,
            shot_count=int(params.get("shot_count") or 8),
            target_seconds=int(params["target_seconds"]) if params.get("target_seconds") else None,
            characters=characters,
            skills=params.get("skills"),
        )
        payload = await _generate_json(
            provider,
            breakdown_prompt,
            system=prompts.SCRIPT_BREAKDOWN_SYSTEM,
            model=model,
            temperature=0.6,
        )
        shots = await _apply_breakdown(session, episode, payload)
        binding = await asset_service.bind_shot_assets(
            session, user_id=task.user_id, project_id=project.id, shots=shots
        )

        # 【角色定妆照】档案落库后立刻给还没有参考图的角色 / 道具各出一张标准参考图，
        # 作为后续分镜生成的默认图像参考。没有参考图时提示词锁不住长相，是「人换了」的根因之一。
        # 串行生成（顺序链）避免同时挤爆图片厂商的并发队列。
        sheet_tasks = await _spawn_asset_reference_tasks(
            session,
            user_id=task.user_id,
            project_id=project.id,
            assets=await asset_service.load_assets_without_reference(
                session, user_id=task.user_id, project_id=project.id
            ),
            aspect_ratio=project.aspect_ratio,
        )
        if sheet_tasks:
            await pipeline_service._start_chain(session, sheet_tasks)

        task.provider = credential.provider
        return {
            "action": action,
            "episode_id": episode.id,
            "title": episode.title,
            "shot_count": len(shots),
            "synopsis": episode.synopsis,
            "characters": characters,
            "assets_bound": binding,
            "reference_tasks": len(sheet_tasks),
        }

    if action == "breakdown":
        episode = await session.get(Episode, int(params["episode_id"]))
        if not episode:
            raise ProviderError("分集不存在")
        project = await session.get(Project, episode.project_id)
        script = (episode.script_text or "").strip()
        if not script:
            raise ProviderError("分集剧本为空，请先填写剧本")
        # 用户在「先规划」阶段确认过的方案：非空时拆解必须按它的段落与镜数展开
        confirmed_plan = (
            prompts.normalize_plan(params["plan"]) if params.get("plan") else None
        )
        # 留空 = 由模型先规划再决定镜头数；非空则作为硬约束
        raw_count = params.get("shot_count")
        shot_count = max(1, min(60, int(raw_count))) if raw_count else None
        if not shot_count and confirmed_plan:
            planned = sum(int(beat.get("shots") or 0) for beat in confirmed_plan["beats"])
            shot_count = planned if 1 <= planned <= 60 else None
        target_seconds = int(params["target_seconds"]) if params.get("target_seconds") else None

        # 【先立角色】直接拆解（没走 forge）的项目没有任何演员资产，后面的关键帧
        # 就拿不到人物参考图，一镜一张脸。所以拆解前先从剧本抽一次角色档案，落成
        # 演员资产并把外观注入分镜提示词；已有档案不覆盖（用户可能手改过外观）。
        style = params.get("style") or (project.style if project else None)
        characters = (
            await asset_service.load_project_characters(
                session, user_id=task.user_id, project_id=project.id
            )
            if project
            else []
        )
        shared_actors = (
            await asset_service.load_shared_actors(session, user_id=task.user_id)
            if project
            else []
        )
        if project and not characters:
            profiles = _drop_library_faces(
                await _extract_character_profiles(
                    provider,
                    script=script,
                    style=style,
                    aspect_ratio=project.aspect_ratio,
                    model=model,
                ),
                shared_actors,
            )
            if profiles:
                characters = await asset_service.ensure_character_assets(
                    session,
                    user_id=task.user_id,
                    project_id=project.id,
                    profiles=profiles,
                    update_existing=False,
                )
        payload = await _breakdown_payload(
            provider,
            model=model,
            title=episode.title,
            script=script,
            style=style,
            aspect_ratio=project.aspect_ratio if project else "9:16",
            shot_count=shot_count,
            target_seconds=target_seconds,
            characters=characters,
            skills=params.get("skills"),
            confirmed_plan=confirmed_plan,
        )
        shots = await _apply_breakdown(session, episode, payload, confirmed_plan=confirmed_plan)
        if project:
            # 模型在镜头里可能点出档案里没有的新角色：补抽一次，让他们也有脸可锁
            known_names = [str(item.get("name") or "") for item in characters]
            known_names += [item.name for item in shared_actors]
            missing_names = _missing_subject_names(shots, known_names)
            if missing_names:
                profiles = _drop_library_faces(
                    await _extract_character_profiles(
                        provider,
                        script=script,
                        style=style,
                        aspect_ratio=project.aspect_ratio,
                        model=model,
                        known=missing_names,
                    ),
                    shared_actors,
                )
                if profiles:
                    await asset_service.ensure_character_assets(
                        session,
                        user_id=task.user_id,
                        project_id=project.id,
                        profiles=profiles,
                        update_existing=False,
                    )
        binding = (
            await asset_service.bind_shot_assets(
                session, user_id=task.user_id, project_id=project.id, shots=shots
            )
            if project
            else {}
        )

        # 【先出脸，关键帧逐镜确认】自动生图只做到定妆照：定妆照不对时重抽成本低，
        # 关键帧则必须逐镜生成、逐镜认可，否则一张错帧会让整批图返工。
        auto_tasks: list[GenerationTask] = []
        sheet_count = 0
        if params.get("auto_images") and project:
            sheet_tasks = await _spawn_asset_reference_tasks(
                session,
                user_id=task.user_id,
                project_id=project.id,
                assets=await asset_service.load_assets_without_reference(
                    session, user_id=task.user_id, project_id=project.id
                ),
                aspect_ratio=project.aspect_ratio,
                credential_id=params.get("credential_id"),
                provider=params.get("provider"),
                model=params.get("model"),
            )
            auto_tasks = sheet_tasks
            sheet_count = len(sheet_tasks)
            if auto_tasks:
                await pipeline_service._start_chain(session, auto_tasks)

        task.provider = credential.provider
        return {
            "action": action,
            "episode_id": episode.id,
            "shot_count": len(shots),
            # 方案要求几镜、实际落库几镜：少了要如实告诉用户，不能悄悄少给
            "planned_shots": shot_count,
            "synopsis": episode.synopsis,
            "plan": episode.plan,
            "assets_bound": binding,
            "reference_tasks": sheet_count,
            "keyframe_tasks": 0,
            "auto_image_tasks": len(auto_tasks),
        }

    raw = await provider.generate_text(
        params.get("prompt") or "",
        system=params.get("system"),
        model=model,
        temperature=float(params.get("temperature") or 0.7),
    )
    task.provider = credential.provider
    return {"action": "custom", "text": raw}


def _shot_directives(item: dict) -> dict | None:
    """抽取 Jellyfish 式镜头指令字段，空则返回 None。"""
    directives = {
        "script_excerpt": item.get("script_excerpt"),
        "shot_size": item.get("shot_size"),
        "camera_angle": item.get("camera_angle"),
        "camera_movement": item.get("camera_movement"),
        "atmosphere": item.get("atmosphere"),
        "mood_tags": item.get("mood_tags"),
        "action_beats": item.get("action_beats"),
        "composition": item.get("composition"),
    }
    cleaned = {k: v for k, v in directives.items() if v not in (None, "", [], {})}
    return cleaned or None


def _camera_of(item: dict) -> str | None:
    """把景别 / 机位 / 运镜拼成镜头语言字符串（兼容旧字段 camera）。"""
    explicit = item.get("camera")
    if explicit:
        return str(explicit)[:200]
    parts = [
        str(item.get(key)).strip()
        for key in ("shot_size", "camera_angle", "camera_movement")
        if item.get(key)
    ]
    return " / ".join(parts)[:200] or None


# 整集一次写完容易被模型的单次输出上限截断（实测 12 镜的方案只落库 8 镜，
# 末镜字段全空），所以方案确认后按叙事段落逐次调用，再合并成一份完整拆解结果。
_BREAKDOWN_CHUNK_SHOTS = 6


def _shot_tail_line(item: dict) -> str:
    """把一段末镜的结束状态压成一行，供下一段开头承接。"""
    head = "，".join(
        part for part in (str(item.get("title") or "").strip(), str(item.get("description") or "").strip()) if part
    )
    state = prompts.build_state_block(None, prompts.normalize_state(item.get("end_state")), lean=True)
    return "；".join(part for part in (head[:200], state.strip()[:300]) if part)


async def _extract_character_profiles(
    provider: BaseProvider,
    *,
    script: str,
    style: str | None,
    aspect_ratio: str | None,
    model: str | None,
    known: list[str] | None = None,
) -> list[dict]:
    """从剧本抽角色外观档案；抽取失败只记日志，不能让整次拆解白跑。"""
    try:
        raw = await _generate_json(
            provider,
            prompts.build_character_profile_prompt(
                script=script,
                style=style,
                aspect_ratio=aspect_ratio or "9:16",
                known=known,
            ),
            system=prompts.CHARACTER_PROFILE_SYSTEM,
            model=model,
            temperature=0.6,
            max_tokens=4096,
        )
    except ProviderError as exc:
        logger.warning("角色档案抽取失败，本次拆解先用文字锁定人物：%s", exc)
        return []
    profiles = prompts.parse_character_profiles(raw)
    # 模型偶尔会补出剧本里没有的人：只保留剧本里点名过的，避免攒出一堆废角色档案
    mentioned = [item for item in profiles if item["name"] in script]
    return mentioned or profiles


def _missing_subject_names(shots: list[Shot], known_names: list[str]) -> list[str]:
    """镜头里点名、但项目 / 资产库里都还没有档案的角色名（保持出现顺序）。"""
    missing: list[str] = []
    for shot in shots:
        for subject in shot.subjects or []:
            if not isinstance(subject, dict) or subject.get("asset_id"):
                continue
            name = str(subject.get("name") or "").strip()
            if not name or name in missing:
                continue
            if any(_same_name(name, known) for known in known_names):
                continue
            missing.append(name)
    return missing


def _drop_library_faces(profiles: list[dict], shared_actors: list) -> list[dict]:
    """资产库里已经有定妆照的同名角色不再建档：否则会凭空多出一张不一样的脸。"""
    looked = [item for item in shared_actors if item.media_id]
    if not looked:
        return profiles
    return [
        item
        for item in profiles
        if not any(_same_name(asset.name, item["name"]) for asset in looked)
    ]


async def _breakdown_payload(
    provider: BaseProvider,
    *,
    model: str | None,
    title: str,
    script: str,
    style: str | None,
    aspect_ratio: str,
    shot_count: int | None,
    target_seconds: int | None,
    characters: list[dict] | None,
    skills: list[str] | None,
    confirmed_plan: dict | None,
) -> dict[str, Any]:
    """按确认过的方案拆解整集：段数多时分段调用，返回合并后的 shots 载荷。"""
    base = {
        "title": title,
        "script": script,
        "style": style,
        "aspect_ratio": aspect_ratio,
        "characters": characters,
        "skills": skills,
        "target_seconds": target_seconds,
    }
    beats = [
        beat
        for beat in ((confirmed_plan or {}).get("beats") or [])
        if isinstance(beat, dict) and _clamp_int(beat.get("shots"), minimum=1, maximum=60, fallback=0) > 0
    ]
    planned = sum(_clamp_int(beat.get("shots"), minimum=1, maximum=60, fallback=0) for beat in beats)
    if len(beats) < 2 or planned <= _BREAKDOWN_CHUNK_SHOTS:
        prompt = prompts.build_breakdown_prompt(**base, shot_count=shot_count, plan=confirmed_plan)
        return await _generate_json(
            provider,
            prompt,
            system=prompts.SCRIPT_BREAKDOWN_SYSTEM,
            model=model,
            temperature=0.6,
        )

    merged: list[dict] = []
    synopsis: str | None = None
    previous: str | None = None
    for index, beat in enumerate(beats, start=1):
        beat_shots = _clamp_int(beat.get("shots"), minimum=1, maximum=60, fallback=1)
        prompt = prompts.build_breakdown_prompt(
            **base,
            shot_count=beat_shots,
            plan=confirmed_plan,
            segment={**beat, "shots": beat_shots, "seg_index": index, "seg_total": len(beats)},
            order_start=len(merged) + 1,
            previous_shot=previous,
        )
        payload = await _generate_json(
            provider,
            prompt,
            system=prompts.SCRIPT_BREAKDOWN_SYSTEM,
            model=model,
            temperature=0.6,
        )
        part = [item for item in (payload.get("shots") or []) if isinstance(item, dict)]
        if not part:
            logger.warning("拆解第 %s/%s 段「%s」没有返回镜头，跳过该段", index, len(beats), beat.get("name"))
            continue
        synopsis = synopsis or (str(payload.get("synopsis") or "").strip() or None)
        for item in part:
            # 每段都从 1 开始编号，落库时按全局顺序重排，避免段间 order_index 撞车
            item.pop("order_index", None)
        merged.extend(part)
        previous = _shot_tail_line(part[-1])
    if not merged:
        raise ProviderError("模型没有按方案输出任何分镜，请重新出方案或减少镜头数后再拆解")
    return {"shots": merged, "synopsis": synopsis, "plan": confirmed_plan}


async def _apply_breakdown(
    session: AsyncSession,
    episode: Episode,
    payload: dict,
    *,
    confirmed_plan: dict | None = None,
) -> list[Shot]:
    existing = list(
        (await session.execute(select(Shot).where(Shot.episode_id == episode.id))).scalars().all()
    )
    # 重跑分镜会把旧镜头整批删掉：旧镜头独占的关键帧图 / 视频也要连文件一起清，
    # 否则素材库里会攒出一堆「已经没人用了但还能被选来当参考图」的废图。
    old_ids = [shot.id for shot in existing]
    owned_media = await reference_service.media_of_shots(session, old_ids)
    for shot in existing:
        await session.delete(shot)
    await session.flush()
    # 文件删除必须等事务提交后再做：中途任何异常回滚后记录会恢复，
    # 但 unlink 掉的文件无法恢复，旧分镜就会指向不存在的图/视频。
    session.info.setdefault("media_files_to_remove", []).extend(
        await reference_service.purge_media(session, owned_media)
    )

    created: list[Shot] = []
    index = 0
    for item in payload.get("shots") or []:
        if not isinstance(item, dict):
            continue
        # 模型输出被长度上限截断时，尾巴上会留一个只有半个字段的空镜头；
        # 这种镜头既不能生图也不能生视频，落库只会让用户看到一条废分镜。
        if not any(
            str(item.get(field) or "").strip()
            for field in ("title", "description", "image_prompt", "video_prompt")
        ):
            logger.warning("跳过没有正文的空分镜（模型输出可能被截断）")
            continue
        index += 1
        context = prompts.parse_shot_context(item)
        shot = Shot(
            episode_id=episode.id,
            order_index=_as_index(item.get("order_index"), index),
            title=item.get("title"),
            description=item.get("description"),
            dialogue=item.get("dialogue"),
            camera=_camera_of(item),
            duration_seconds=_as_float(item.get("duration_seconds")) or 5.0,
            resolution=_normalize_resolution(item.get("resolution")),
            image_prompt=item.get("image_prompt"),
            video_prompt=item.get("video_prompt"),
            directives=_shot_directives(item),
            subjects=context["subjects"] or None,
            props=context["props"] or None,
            action_timeline=context["action_timeline"] or None,
            start_state=context["start_state"],
            end_state=context["end_state"],
            status="pending",
        )
        session.add(shot)
        created.append(shot)

    await session.flush()
    if payload.get("synopsis"):
        episode.synopsis = payload["synopsis"]
    # 规划与分镜同源：落库后前端可展示「先按哪几段拆、每段几镜、为什么」。
    # 用户确认过方案时以确认版为准，后续「重新拆解」也能看出上一版是怎么定的。
    plan = prompts.normalize_plan(confirmed_plan) if confirmed_plan else prompts.normalize_plan(payload)
    episode.plan = (
        {**plan, "shot_count": len(created), "confirmed": bool(confirmed_plan)}
        if plan
        else None
    )
    episode.status = "storyboarded"
    return created


async def _handle_image(session: AsyncSession, task: GenerationTask) -> dict[str, Any]:
    params = task.params or {}
    flow = str(params.get("flow") or "")
    # 资产参考图（定妆三视图 / 道具单件图）不是分镜画面：不能套用「起幅 / 人物站位」
    # 和人体结构那套镜头质量块，否则道具图会被补上一个人（实测三张全变人像）。
    is_asset_sheet = flow == "asset_reference"
    shot = await session.get(Shot, int(params["shot_id"])) if params.get("shot_id") else None

    credentials = await resolve_fallback_chain(
        session, task.user_id, "image", params.get("credential_id"), params.get("provider") or task.provider
    )

    project = await _project_of_shot(session, shot)
    # 资产参考图任务没有分镜，此时项目从 params 取，以便沿用该项目的画幅与画风
    if not project and params.get("project_id"):
        project = await session.get(Project, int(params["project_id"]))
    aspect_ratio = params.get("aspect_ratio") or (project.aspect_ratio if project else "9:16")
    style_text = _project_style_text(project)

    base_prompt = (
        params.get("prompt")
        or (shot.image_prompt if shot else None)
        or (shot.description if shot else None)
        or ""
    ).strip()
    # 带模板时用模板渲染出主体提示词（默认带入项目风格 / 画幅 / 主体描述）
    base_prompt = await _base_prompt_with_template(
        session,
        task=task,
        shot=shot,
        params=params,
        aspect_ratio=aspect_ratio,
        project=project,
        fallback=base_prompt,
    )
    if not base_prompt:
        base_prompt = f"{shot.title or ''} {style_text or ''}".strip() if shot else ""
    if not base_prompt:
        raise ProviderError("缺少图片提示词，请先完善分镜描述或填写提示词")

    assets = await _load_shot_assets(
        session, task.user_id, shot=shot, asset_ids=params.get("asset_ids")
    )
    continuity = await _continuity_block(session, shot, field="image_prompt") if shot else None
    shot_context = _shot_prompt_context(shot)

    # 【关键帧链式衔接】「新增关键帧」（非重绘某张）时，把该镜头已有最后一张关键帧的画面
    # 承接进来，让第 N 张自动从第 N-1 张的结尾状态继续，而不是每次都用同一句提示词。
    existing_kfs: list = []
    is_new_keyframe = shot is not None and not params.get("keyframe_id")
    if is_new_keyframe:
        existing_kfs = (await load_keyframes(session, [shot.id])).get(shot.id, [])

    if shot:
        shot.status = "generating"
        await session.flush()

    # n>1 时循环生成多张：不同 provider 对 n 参数支持不一，统一在调用侧循环，
    # 每张独立 persist + attach 为一张新关键帧，避免只落第一张。
    want_n = int(params.get("n") or 1)
    replaced_media: list[int] = []
    if params.get("keyframe_id"):
        want_n = 1  # 重绘指定关键帧只出 1 张
        # 重绘会换掉这张上的旧图；旧图若已无其他引用就一并清掉，素材库里不会堆废片
        old_kf = await session.get(ShotKeyframe, int(params["keyframe_id"]))
        if old_kf is not None and old_kf.media_id:
            replaced_media.append(int(old_kf.media_id))

    # 单张、且没指定要重绘哪一张 = 这一张要替换该镜头现有的画面（改完提示词重出、流水线重跑这批镜头）。
    # 此时不能再拿本镜头那张旧图当参考、还写「必须与上一张保持一致」——那等于逼模型复刻有缺陷的画面。
    # ``append`` = 用户明确要「生成下一张」：新增一张并承接上一张的结尾状态，不替换现有画面。
    replaces_frame = is_new_keyframe and want_n == 1 and not params.get("append")

    if not credentials:
        raise ProviderError("图片生成失败：未配置可用的图片凭证")
    credential = credentials[0]
    provider = build_provider_from(credential)

    # 【资产一致性】把关联资产的参考图作为「图像参考」送进模型（图生图）。
    # 只靠文字描述锁不住五官，这是让同一角色在不同镜头里长得一样的唯一可靠手段。
    # 按本镜头 subjects 选择参考图，并生成编号图例；厂商未声明支持图像参考时不传。
    supports_reference = bool(getattr(provider, "supports_image_reference", False))
    references = (
        _pick_references(assets, shot=shot, explicit_ids=params.get("asset_ids"))
        if supports_reference
        else []
    )
    # 【参考图策略】多张棚拍定妆照一起送，Agnes 经常把人物复制一份、或把教室画成客厅；
    # 因此按证据收紧：同一场景同一组人有上一帧时，只用上一帧锁脸/服装/场景；
    # 没有上一帧时，多人镜头不送定妆照（纯文生图更听话），单人镜头才送一张锁脸。
    # 道具 / 场景参考图不影响人物数量，始终保留。用户显式勾选的资产不被过滤。
    empty_shot = shot is not None and not _named_subject_set(shot)
    # 「生成下一张」且已有上一张时，唯一该送的参考就是本镜头上一张（链式承接在后面拼）；
    # 再叠上一镜的连续性参考会变成两张构图模板，模型容易照抄站位。
    append_mode = bool(params.get("append")) and bool(existing_kfs) and not params.get("asset_ids")
    if shot and supports_reference and not params.get("asset_ids"):
        previous = await _previous_shot_reference(session, shot)
        non_actor = [item for item in references if item.get("kind") != "actor"]
        object_refs = [
            item for item in non_actor if not _is_scene_element(str(item.get("name") or ""))
        ]
        actor_refs = [item for item in references if item.get("kind") == "actor"]
        subject_count = len(_named_subject_set(shot))
        if append_mode:
            references = []
        elif previous is not None:
            # 同一场戏同一组人的上一帧，比多张定妆照更能锁住脸、服装、场景与光线；
            # 只送这一张，避免定妆照/道具产品图被模型当成第二个构图模板。
            references = [previous]
        elif subject_count == 0:
            # 空镜不送场景构件的产品图（灰底门/窗照片会把教室画成摄影棚或别的走廊）
            references = object_refs
        elif subject_count == 1 and _hides_face(shot):
            # 背影镜头看不到脸，定妆照唯一的作用是让模型照着参考图再复制一个人
            references = []
        else:
            # 【多人镜头 · 多参考 + 防分身】没有上一帧时，按角色各送一张单人定妆照，
            # 让模型分别锁定每张脸；群像定妆照仍不送（一张图里好几个人，会被当成构图
            # 模板按人数复制）。图例与【人数硬约束】会同时声明「参考图张数≠画面人数」。
            solo_actor_refs = [item for item in actor_refs if not item.get("group")]
            group_in_cast = any(item.get("group") for item in actor_refs)
            if solo_actor_refs:
                references = (solo_actor_refs + object_refs)[:4]
            elif not group_in_cast:
                references = object_refs
            else:
                references = []
    elif shot and supports_reference:
        previous = await _previous_shot_reference(session, shot)
        if previous and all(item["url"] != previous["url"] for item in references):
            if len(references) < 4:
                references.append(previous)
            else:
                references[-1] = previous

    # 同镜头多张关键帧的链式参考：第 N 张承接第 N-1 张的结尾状态
    chain_reference: dict | None = None
    if supports_reference and is_new_keyframe and existing_kfs and not replaces_frame:
        chain_reference = await _media_reference(
            session,
            existing_kfs[-1].media_id,
            name="本镜头上一张关键帧",
            purpose=(
                "承接上一张结尾的人物位置、服装、场景与光线，人物五官脸型胖瘦不得改变，"
                "画面推进到下一阶段，不要雷同"
            ),
        )

    # 【资产改设定】给已有参考图的资产重新出图时（典型场景：主角换衣服但脸不能变），
    # 必须把资产现有的参考图作为「待修改的既有形象」送进模型做图生图；
    # 只给文字描述的话模型会按描述重新编一张脸，人物就换了。
    identity_reference: dict | None = None
    reason_no_reference: str | None = None
    # 给已有参考图的资产改设定时，本次必须真的用上既有形象；平台降级成纯文生图时
    # 若照常把结果挂回资产，会静默换掉这张脸并删掉旧参考图，损失无法回滚。
    asset_reference_required = False
    target_asset_id = params.get("asset_id")
    if target_asset_id:
        if not supports_reference:
            reason_no_reference = f"当前厂商（{credential.provider}）不支持图像参考，无法基于已有形象修改，只能重新文生图。"
        elif params.get("use_reference", True):
            self_items = await _load_assets(session, task.user_id, [target_asset_id])
            asset_reference_required = bool(
                self_items
                and (self_items[0].get("reference_url") or self_items[0].get("reference_missing"))
            )
            if self_items and self_items[0].get("reference_url"):
                identity_reference = {
                    "kind": "identity",
                    "name": self_items[0].get("name"),
                    "purpose": "仅保持五官、脸型、发型与肤色；服装 / 配饰 / 造型按本次描述修改",
                    "url": self_items[0]["reference_url"],
                }
            elif self_items and self_items[0].get("reference_missing"):
                reason_no_reference = (
                    f"资产「{self_items[0].get('name')}」已有参考图，但拿不到公网地址无法送进模型；"
                    "请配置 PUBLIC_MEDIA_BASE_URL，否则无法在既有形象上修改。"
                )
    # 用户手动指定的额外参考图（从素材库挑一张，如某套服装的实物图）
    extra_media_reference: dict | None = None
    if supports_reference and params.get("reference_media_id"):
        extra_media_reference = await _media_reference(
            session,
            params.get("reference_media_id"),
            name="用户指定的参考图",
            purpose="参考其服装 / 配饰 / 造型，人物五官仍以上一张既有形象为准",
            kind="identity",
        )

    # 既有形象始终排在第一位（图1），保证模型优先锁定这张脸
    for item in (extra_media_reference, identity_reference):
        if not item:
            continue
        if all(exist["url"] != item["url"] for exist in references):
            references.insert(0, item)
    references = references[:4]

    # 【参考图体检】逐个人物核对「本帧到底有没有脸可锁」：
    # 没建档 / 定妆照没出 / 图拿不到公网地址，都会让该角色退回纯文字描述，
    # 这正是「关键帧不像本人」的根因。结果随关键帧一起存下来并在前端展示。
    reference_issues: list[str] = []
    has_continuity_ref = append_mode or any(
        item.get("kind") == "continuity" for item in references
    )
    if shot is not None and supports_reference and not has_continuity_ref:
        actor_assets = [item for item in assets if item.get("kind") == "actor"]
        for name in sorted(_named_subject_set(shot)):
            match = next(
                (item for item in actor_assets if _same_name(item.get("name"), name)), None
            )
            if match is None:
                reference_issues.append(f"{name}：没有角色档案，无法锁脸")
            elif not match.get("media_id"):
                reference_issues.append(f"{name}：定妆照还没生成，无法锁脸")
            elif not match.get("reference_url"):
                reference_issues.append(f"{name}：参考图拿不到公网地址，送不进模型")

    # 本次是「改设定」而不是「重画」时，明确告诉模型保持五官、只改服装
    identity_extra: str | None = None
    if identity_reference:
        identity_extra = (
            "【本次为既有形象修改，不是重新设计角色】图1是该角色已经确定的既有形象，"
            "必须严格保持其五官、脸型、发型、肤色与人物比例不变，只按上面的描述修改服装、配饰与造型；"
            "不要重新编造长相，也不要改变性别与年龄段。"
        )

    task.provider = credential.provider
    task.model = params.get("model") or task.model

    # batch=3 时自动用「起幅 / 中段 / 落幅」三阶段标签
    stage_labels = ["起幅", "中段", "落幅", "关键帧4", "关键帧5", "关键帧6"]
    stage_hints = {
        0: "本镜头起幅：交代环境、人物站位与景别",
        1: "本镜头中段：动作推进到最有张力的瞬间",
        2: "本镜头落幅：情绪收束，画面定格",
    }
    keyframe_prompts = [str(item).strip() for item in (params.get("keyframe_prompts") or [])]

    created_assets: list = []
    prev_clean: str | None = None
    used_any_reference = False
    for i in range(want_n):
        # 逐张提示词优先：批量时第 i 张用 keyframe_prompts[i]；未给则该张回落到基础提示词 + 阶段说明
        custom = keyframe_prompts[i] if i < len(keyframe_prompts) and keyframe_prompts[i] else ""
        round_base = custom or base_prompt
        extra = None if is_asset_sheet else stage_hints.get(i)
        if identity_extra:
            extra = f"{extra}\n{identity_extra}" if extra else identity_extra
        # 链式衔接：承接本镜头上一张的结尾状态（第一次承接已有最后一张，后续承接刚生成的上一张）
        if is_new_keyframe and not replaces_frame:
            prev_desc = prev_clean or (
                (existing_kfs[-1].prompt or existing_kfs[-1].label or "").strip()
                if existing_kfs
                else ""
            )
            if prev_desc:
                index_label = len(existing_kfs) + i + 1
                link = (
                    f"【本镜头第 {index_label} 张关键帧 · 链式衔接】上一张画面是：{prev_desc}。"
                    "本张必须承接上一张的结尾状态：人物位置、服装、场景、光线保持一致；"
                    "画面瞬间推进到动作的下一阶段或情绪的递进，不要与上一张雷同。"
                )
                extra = f"{extra}\n{link}" if extra else link

        # 参考图逐轮重算：第 2 张起要承接刚生成的上一张，而不是只为第一张准备的那批
        round_references = list(references)
        if chain_reference and all(
            item["url"] != chain_reference["url"] for item in round_references
        ):
            if len(round_references) < 4:
                round_references.append(chain_reference)
            else:
                round_references[-1] = chain_reference
        round_urls = [item["url"] for item in round_references]

        full_prompt = prompts.compose_generation_prompt(
            base=round_base,
            kind="image",
            style=None if is_asset_sheet else style_text,
            aspect_ratio=aspect_ratio,
            assets=assets,
            negative=params.get("negative_prompt"),
            continuity=continuity,
            extra=extra,
            with_quality=not is_asset_sheet,
            references=round_references,
            empty_shot=empty_shot,
            # 角色定妆 / 多视角设定图不带分镜上下文，本身很短，不必再走预算裁剪
            max_chars=None if is_asset_sheet else prompts.MAX_IMAGE_PROMPT_CHARS,
            **(shot_context if not is_asset_sheet else {}),
        )
        try:
            result = await provider.generate_image(
                full_prompt,
                model=params.get("model") or task.model,
                size=params.get("size"),
                n=1,
                aspect_ratio=aspect_ratio,
                reference_image_urls=round_urls or None,
                on_progress=progress_callback(task.id),
            )
        except Exception as exc:  # noqa: BLE001
            if created_assets:
                break
            raise
        if asset_reference_required and not bool((result.meta or {}).get("reference_used", True)):
            raise ProviderError(
                "资产改设定时参考图未能送达模型（平台已降级为纯文生图）。"
                "为避免覆盖资产现有参考图，本次结果已丢弃，请稍后重试。"
            )
        task.model = result.meta.get("model") or task.model
        used_this_round = (
            bool((result.meta or {}).get("reference_used", True)) if round_references else None
        )
        reference_warning = None
        if round_references and not used_this_round:
            reference_warning = (
                "参考图队列繁忙，本次已降级为纯文生图，人物/道具一致性可能下降；建议稍后重绘该帧。"
            )
        asset = await storage.persist_media(
            session,
            user_id=task.user_id,
            kind="image",
            provider=credential.provider,
            model=task.model,
            project_id=task.project_id,
            shot_id=shot.id if shot else None,
            task_id=task.id,
            url=result.url,
            data=result.data,
            mime_type=result.mime_type,
            # prompt_clean：该张实际使用的主体提示词（干净版），「存为资产」时优先取它；
            # prompt_used：含资产 / 画质 / 衔接 / 负面的完整指令，便于排查与复现。
            # reference_images / reference_roles：本次实际送进模型的参考图与对应身份，
            # 是排查「角色不一致 / 伞换人拿」的关键证据。
            meta={
                **(result.meta or {}),
                "prompt_clean": round_base,
                "prompt_used": full_prompt,
                "reference_images": round_urls,
                "reference_roles": [
                    {"kind": item.get("kind"), "name": item.get("name")}
                    for item in round_references
                ],
                "reference_count": len(round_references),
                "reference_used": used_this_round,
                "reference_warning": reference_warning,
                "reference_issues": reference_issues,
            },
        )
        created_assets.append(asset)
        prev_clean = round_base
        if used_this_round:
            used_any_reference = True
        # 同一批多张时，下一张以这一张为链式参考；重绘指定关键帧不改变后续基准
        if is_new_keyframe and supports_reference:
            chain_reference = await _media_reference(
                session,
                asset.id,
                name="本镜头上一张关键帧",
                purpose="承接上一张结尾的人物位置、服装、场景与光线，画面推进到下一阶段，不要雷同",
            )
        if shot:
            auto_label = (
                stage_labels[i] if i < len(stage_labels) else f"关键帧{len(existing_kfs) + i + 1}"
            )
            await attach_generated_image(
                session,
                shot=shot,
                media_id=asset.id,
                prompt=round_base,
                keyframe_id=params.get("keyframe_id"),
                label=(params.get("keyframe_label") if want_n == 1 else auto_label),
                # 单张出图且没指定要重绘哪张 = 这一张就是要替换该镜头当前的画面，必须接管主帧；
                # 多张（起幅/中段/落幅）是同一镜头的推进序列，首帧仍是主帧，不能改。
                # 「生成下一张」也不接管主帧：主帧停留在用户已认可的那一张。
                promote_primary=(
                    want_n == 1 and not params.get("keyframe_id") and not params.get("append")
                ),
            )

    if not created_assets:
        raise ProviderError("图片生成失败：未产出任何图片")

    first = created_assets[0]
    payload: dict[str, Any] = {
        "media_id": first.id,
        "url": first.url,
        "kind": "image",
        "media_ids": [a.id for a in created_assets],
        "urls": [a.url for a in created_assets],
        "count": len(created_assets),
    }

    # 【AI 初审】单张关键帧出图后顺带做一次读图审查（只给建议，绝不自动认可）；
    # 多张批次（起幅/中段/落幅）不逐张调，避免一次生图产生多笔视觉调用。
    # 审查失败只记日志，不影响生图任务本身。
    if shot is not None and not is_asset_sheet and len(created_assets) == 1:
        from app.services.agents.visual_qa import maybe_review_keyframe

        review = await maybe_review_keyframe(
            session, user_id=task.user_id, asset=first, shot=shot
        )
        if review:
            payload["visual_review"] = review

    if references or chain_reference:
        payload["reference_used"] = used_any_reference
        payload["reference_count"] = len(references) + (1 if chain_reference else 0)
        payload["reference_roles"] = [
            {"kind": item.get("kind"), "name": item.get("name")} for item in references
        ]
        if chain_reference:
            payload["reference_roles"].append(
                {"kind": chain_reference.get("kind"), "name": chain_reference.get("name")}
            )
        if not used_any_reference:
            payload["warning"] = (
                "参考图队列繁忙，本次已降级为纯文生图，角色/道具一致性可能下降；"
                "建议稍后重试该镜头。"
            )
    if reference_issues:
        payload["reference_issues"] = reference_issues

    # 参考图已生成但送不出去（本地部署未配置 PUBLIC_MEDIA_BASE_URL）时给明确提示，
    # 否则用户只会看到「人物又变了」却查不到原因。
    missing_refs = [item.get("name") for item in (assets or []) if item.get("reference_missing")]
    if missing_refs:
        payload["reference_hint"] = (
            f"以下资产已有参考图但无法送进模型：{'、'.join(str(name) for name in missing_refs)}。"
            "本地部署需配置 PUBLIC_MEDIA_BASE_URL（对外可访问的媒体地址），模型才能取到参考图；"
            "否则本次只能用文字描述锁定外观。"
        )

    # 资产改设定（换衣服但脸不能变）没能用上既有形象时，明确说明原因，
    # 避免用户以为「已经按原图改了」却发现人物又变了。
    if reason_no_reference:
        payload["reference_hint"] = reason_no_reference
        payload["reference_used"] = False
    elif identity_reference:
        payload["reference_used"] = used_any_reference
        payload["identity_reference"] = identity_reference["url"]

    # 【资产生图】本次是给「角色 / 场景」资产出参考图时，把成品图挂回该资产，
    # 并把本次描述词写成资产的外观描述。之后在分镜里关联这个资产，
    # 这张图就会作为图像参考一起送进模型，保证跨镜头是同一个角色 / 场景。
    asset_id = params.get("asset_id")
    if asset_id:
        target = await session.get(ProductionAsset, int(asset_id))
        if target and target.user_id == task.user_id:
            if target.media_id and target.media_id != first.id:
                replaced_media.append(int(target.media_id))
            target.media_id = first.id
            if params.get("prompt"):
                target.prompt = str(params["prompt"]).strip() or target.prompt
            if not target.description:
                target.description = "由 AI 依据外观描述生成的参考图"
            await session.commit()
            payload["asset_id"] = target.id

    if replaced_media:
        # 新图已挂上，被替换掉的旧图若不再被任何地方引用就删掉（记录 + 磁盘文件）
        paths = await reference_service.purge_media(session, replaced_media)
        await session.commit()
        reference_service.remove_media_files(paths)

    return payload


async def _handle_video(session: AsyncSession, task: GenerationTask) -> dict[str, Any]:
    params = task.params or {}
    shot = await session.get(Shot, int(params["shot_id"])) if params.get("shot_id") else None
    # 重新生成视频会换掉旧的那段；旧片段若无其他引用则一并删除，素材库不留废片
    replaced_media: list[int] = []
    replaced_video = shot.video_media_id if shot else None

    credentials = await resolve_fallback_chain(
        session, task.user_id, "video", params.get("credential_id"), params.get("provider") or task.provider
    )

    project = await _project_of_shot(session, shot)
    aspect_ratio = params.get("aspect_ratio") or (project.aspect_ratio if project else "9:16")
    style_text = _project_style_text(project)

    explicit_prompt = params.get("prompt")
    base_prompt = (
        explicit_prompt
        or (shot.video_prompt if shot else None)
        or (shot.description if shot else None)
        or ""
    ).strip()
    # 自动生成的 video_prompt 已含秒级动作，时间轴块会完整再发一次；
    # 用户显式填写的 prompt 保持原样，只去重分镜自动生成的那份。
    if not explicit_prompt and shot is not None and shot.action_timeline:
        base_prompt = _strip_timed_beats(base_prompt)
    base_prompt = await _base_prompt_with_template(
        session,
        task=task,
        shot=shot,
        params=params,
        aspect_ratio=aspect_ratio,
        project=project,
        fallback=base_prompt,
    )
    if not base_prompt:
        raise ProviderError("缺少视频提示词，请先完善分镜描述或填写提示词")

    duration = int(params.get("duration") or (shot.duration_seconds if shot else 5) or 5)
    shot_context = _shot_prompt_context(shot)
    shot_context["duration"] = duration
    empty_shot = shot is not None and not _named_subject_set(shot)
    # 统一按「主体 → 资产一致性 → 时间轴 → 首尾状态 → 视频质量 → 前后镜头衔接 → 负面提示」拼装完整指令
    prompt = prompts.compose_generation_prompt(
        base=base_prompt,
        kind="video",
        style=style_text,
        aspect_ratio=aspect_ratio,
        assets=await _load_shot_assets(
            session, task.user_id, shot=shot, asset_ids=params.get("asset_ids")
        ),
        negative=params.get("negative_prompt"),
        continuity=await _video_continuity_block(session, shot) if shot else None,
        max_chars=prompts.MAX_VIDEO_PROMPT_CHARS,
        empty_shot=empty_shot,
        **shot_context,
    )
    # 同场合同组人物、下一镜首帧可公网访问时，用首尾帧把两段视频焊在一起；
    # 换场/换人的硬切镜头不传，否则模型会在段内把场景渐变过去。
    next_frame_url = await _next_continuation_frame(session, shot) if shot else None
    seed = project.seed if project else None

    # 关键帧：不同厂商对首帧取法不同——agnes 需要公网可访问的图片 URL，
    # magichour 可直接接收图片字节（本地素材无需暴露内网地址）。
    # 未显式指定 image_media_id 时，取该分镜的主关键帧（若主帧尚未出图，顺位取第一张已出图的）。
    keyframe: MediaAsset | None = None
    keyframe_id = params.get("image_media_id")
    if keyframe_id is None and shot is not None:
        keyframes = (await load_keyframes(session, [shot.id])).get(shot.id, [])
        primary = primary_keyframe(keyframes)
        keyframe_id = primary.media_id if primary else None
    if keyframe_id:
        keyframe = await session.get(MediaAsset, int(keyframe_id))
    image_url = (
        ((keyframe.meta or {}).get("source_url") or storage.to_public_url(keyframe.url))
        if keyframe
        else None
    )

    if shot:
        shot.status = "generating"
        await session.flush()

    # 依次尝试候选厂商：按当前候选厂商决定首帧取法；仅临时故障才回退到下一个候选。
    fallbacks: list[dict[str, Any]] = []
    result = None
    credential: ProviderCredential | None = None
    for index, candidate in enumerate(credentials):
        provider = build_provider_from(candidate)

        candidate_url = image_url
        candidate_data: bytes | None = None
        candidate_filename: str | None = None
        if keyframe is not None:
            if candidate.provider == "agnes" and not candidate_url:
                # Agnes 图生视频强制要求公网可访问首帧；本地素材拿不到公网地址时该候选不可用，
                # 记录原因后尝试下一个候选，而不是直接整体失败。
                reason = (
                    "Agnes 图生视频要求首帧为公网可访问的图片地址，本地素材无法提供，"
                    "请配置 PUBLIC_MEDIA_BASE_URL 或改用其它厂商。"
                )
                if index < len(credentials) - 1:
                    fallbacks.append(
                        {"provider": candidate.provider, "credential_id": candidate.id, "error": reason}
                    )
                    continue
                raise ProviderError(reason, transient=False)
            if candidate.provider == "magichour" and not candidate_url:
                # Magic Hour 支持直接上传图片字节，本地素材无需公网地址
                candidate_data = await storage.asset_bytes(keyframe)
                candidate_filename = keyframe.filename

        try:
            result = await provider.generate_video(
                prompt,
                model=params.get("model") or task.model,
                image_url=candidate_url,
                image_data=candidate_data,
                image_filename=candidate_filename,
                last_frame_url=next_frame_url,
                seed=seed,
                duration=duration,
                aspect_ratio=aspect_ratio,
                resolution=params.get("resolution"),
                on_progress=progress_callback(task.id),
            )
        except Exception as exc:  # noqa: BLE001 - 需区分临时故障与参数/鉴权错误
            if is_transient_error(exc) and index < len(credentials) - 1:
                fallbacks.append(
                    {"provider": candidate.provider, "credential_id": candidate.id, "error": str(exc)}
                )
                continue
            raise
        credential = candidate
        break

    if result is None or credential is None:
        raise ProviderError("视频生成失败：候选厂商均不可用")

    task.provider = credential.provider
    task.model = result.meta.get("model") or task.model

    asset = await storage.persist_media(
        session,
        user_id=task.user_id,
        kind="video",
        provider=credential.provider,
        model=task.model,
        project_id=task.project_id,
        shot_id=shot.id if shot else None,
        task_id=task.id,
        url=result.url,
        data=result.data,
        mime_type=result.mime_type,
        meta={
            **(result.meta or {}),
            "prompt_clean": base_prompt,
            "prompt_used": prompt,
            # 留痕：本段视频实际用的首/尾帧与随机种子，排查「两段视频接不上」时的关键证据。
            "first_frame_url": candidate_url,
            "last_frame_url": next_frame_url,
            "seed": seed,
        },
        duration_seconds=_as_float(result.meta.get("duration")),
    )
    if shot:
        if replaced_video and replaced_video != asset.id:
            replaced_media.append(int(replaced_video))
        shot.video_media_id = asset.id
        shot.status = "ready"

    payload: dict[str, Any] = {"media_id": asset.id, "url": asset.url, "kind": "video"}
    if fallbacks:
        payload["fallbacks"] = fallbacks
    if replaced_media:
        paths = await reference_service.purge_media(session, replaced_media)
        await session.commit()
        reference_service.remove_media_files(paths)
    return payload


async def _handle_export(session: AsyncSession, task: GenerationTask) -> dict[str, Any]:
    # 切点审查先跑：dry-run 只出报告，任何失败都不阻断导出
    continuity = None
    if task.project_id:
        from app.services.agents.continuity import maybe_review_project

        continuity = await maybe_review_project(
            session, user_id=task.user_id, project_id=task.project_id
        )
    result = await export_service.export_project(session, task)
    if continuity is not None:
        result["continuity"] = continuity
    if task.episode_id:
        episode = await session.get(Episode, int(task.episode_id))
        if episode:
            episode.status = "completed"
    elif task.project_id:
        # 项目级导出成功后，分集状态要跟着收敛，否则分集永远停在 generating
        await session.execute(
            update(Episode)
            .where(Episode.project_id == task.project_id, Episode.status == "generating")
            .values(status="completed")
        )
    run_id = (task.params or {}).get("run_id")
    if run_id:
        run = await session.get(PipelineRun, int(run_id))
        if run:
            run.status = "completed"
            run.stage = "done"
            run.error = None
            result["pipeline_run_id"] = run.id
    elif task.project_id:
        # 单独触发的导出成功后，也要把该项目遗留的失败流水线收敛掉：
        # 否则流水线卡片会一直挂着上次的报错（用户实测反馈）。
        run = (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.project_id == task.project_id, PipelineRun.status == "failed")
                .order_by(PipelineRun.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if run:
            run.status = "completed"
            run.stage = "done"
            run.error = None
            result["pipeline_run_id"] = run.id
    return result


async def _handle_pipeline(session: AsyncSession, task: GenerationTask) -> dict[str, Any]:
    """制作流水线任务：按 stage 跑一遍 LangGraph 编排（建角色档案 / 出分镜 / 批量生图）。"""
    params = task.params or {}
    run_id = int(params.get("run_id") or 0)
    if not run_id:
        raise ProviderError("流水线任务缺少 run_id")
    stage = params.get("stage") or "start"
    await pipeline_service.run_pipeline(run_id, stage)
    return {"action": "pipeline", "run_id": run_id, "stage": stage}


async def _handle_edit(session, task):  # noqa: ANN001 - 与其它 handler 同签名
    """智能剪辑：延迟导入，避免 generation ↔ editing 循环依赖。"""
    from app.services.editing import execute_edit

    return await execute_edit(session, task)


_HANDLERS: dict[str, Callable[..., Any]] = {
    "text": _handle_text,
    "image": _handle_image,
    "video": _handle_video,
    "export": _handle_export,
    "pipeline": _handle_pipeline,
    "edit": _handle_edit,
}


# ---------------------------------------------------------------- 任务执行入口
async def execute_task(task_id: int) -> None:
    async with AsyncSessionLocal() as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return

        if task.status in ("succeeded", "canceled"):
            return

        # 提前快照：commit 会让 ORM 属性过期，flush 失败后再读会触发惰性加载而报错
        failed_shot_id = task.shot_id

        task.status = "running"
        task.started_at = _now()
        task.progress = max(task.progress or 0, 5)
        task.error = None
        await session.commit()

        handler = _HANDLERS.get(task.kind)
        if handler is None:
            await _finalize(session, task_id, status="failed", error=f"不支持的任务类型：{task.kind}")
            await _advance_chain(session, task_id, succeeded=False, error=f"不支持的任务类型：{task.kind}")
            return

        try:
            result = await handler(session, task)
            await session.commit()
            # 事务已落库，此时再删文件不会出现「记录回滚、文件已丢」的不可逆状态
            reference_service.remove_media_files(session.info.pop("media_files_to_remove", []))
        except Exception as exc:  # noqa: BLE001 - 需落库为任务失败
            await session.rollback()
            message = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
            if failed_shot_id:
                await session.execute(
                    update(Shot).where(Shot.id == failed_shot_id).values(status="failed")
                )
            await _finalize(session, task_id, status="failed", error=message[:2000])
            await _advance_chain(session, task_id, succeeded=False, error=message[:500])
            return

        await _finalize(session, task_id, status="succeeded", result=result)
        await _advance_chain(session, task_id, succeeded=True)


async def _advance_chain(
    session: AsyncSession,
    task_id: int,
    *,
    succeeded: bool,
    error: str | None = None,
) -> None:
    """顺序链推进：投递下一个任务；链尾则收敛所属流水线状态。

    批量生图必须串行——第 N 个镜头要把第 N-1 个镜头的关键帧当参考图，
    并发跑会让「上一镜头的结尾画面」还没落库就被引用，连续性失效。
    """
    # 延迟导入：worker.tasks 反向依赖本模块，模块级 import 会成环
    from app.services.dispatch import enqueue_task

    task = await session.get(GenerationTask, task_id)
    if not task:
        return
    params = task.params or {}
    flow = params.get("flow")
    next_id = params.get("chain_next")
    if next_id:
        if not succeeded and flow in _VIDEO_CHAIN_FLOWS:
            # 视频链失败后继续推进只会在链尾得到一部缺镜头的成片，
            # 因此直接作废后续任务，并把流水线收敛为失败。
            nxt = await session.get(GenerationTask, int(next_id))
            await _abort_chain(session, nxt, error or "上游镜头生成失败，已停止后续生成与导出")
            return
        nxt = await session.get(GenerationTask, int(next_id))
        if nxt and nxt.status == "pending":
            try:
                await enqueue_task(session, nxt)
            except Exception as exc:  # noqa: BLE001 - 队列异常时链路不能悬空
                nxt.status = "failed"
                nxt.error = f"任务队列不可用：{exc}"
                await session.commit()
                await _advance_chain(session, nxt.id, succeeded=False, error=str(exc))
        return

    run_id = params.get("run_id")
    if not run_id or not flow:
        return
    run = await session.get(PipelineRun, int(run_id))
    if not run:
        return

    if flow == "pipeline_images":
        failed = 0
        if run.episode_id:
            failed = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Shot)
                        .where(Shot.episode_id == run.episode_id, Shot.status == "failed")
                    )
                ).scalar_one()
            )
        run.status = "images_ready"
        run.stage = "images_ready"
        run.error = (
            f"有 {failed} 个镜头生图失败，可在分镜页单独重跑后再继续生视频" if failed else None
        )
        await session.commit()
    elif flow == "pipeline_videos":
        # 正常会在链尾接上导出任务；没有导出任务说明项目还有镜头没出视频
        missing = 0
        if run.project_id:
            missing = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Shot)
                        .join(Episode, Shot.episode_id == Episode.id)
                        .where(
                            Episode.project_id == run.project_id,
                            Shot.video_media_id.is_(None),
                        )
                    )
                ).scalar_one()
            )
        if not succeeded:
            run.status = "failed"
            run.stage = "failed"
        elif missing == 0:
            run.status = "completed"
            run.stage = "done"
        else:
            run.status = "videos_ready"
            run.stage = "videos_ready"
        run.error = None if succeeded else (error or "视频生成失败")[:1000]
        await session.commit()
    elif flow == "pipeline_export":
        if succeeded:
            run.status = "completed"
            run.stage = "done"
            run.error = None
        else:
            run.status = "failed"
            run.stage = "failed"
            run.error = (error or "成片导出失败")[:1000]
        await session.commit()


# 这两个流程的链尾是自动导出：中途失败还继续推进，只会得到缺镜头的成片。
_VIDEO_CHAIN_FLOWS = ("pipeline_videos", "pipeline_export")


async def _fail_run(session: AsyncSession, params: dict | None, reason: str) -> None:
    run_id = (params or {}).get("run_id")
    if not run_id:
        return
    run = await session.get(PipelineRun, int(run_id))
    if run:
        run.status = "failed"
        run.stage = "failed"
        run.error = (reason or "任务失败")[:1000]
        await session.commit()


async def _abort_chain(session: AsyncSession, first: GenerationTask | None, reason: str) -> None:
    """作废一条链上所有尚未执行的后续任务，并归档流水线为失败。"""
    current = first
    while current is not None and current.status == "pending":
        current.status = "canceled"
        current.error = reason[:2000]
        current.finished_at = _now()
        if current.shot_id:
            # 这些镜头从未开始生成，退回 pending 而不是 failed
            await session.execute(update(Shot).where(Shot.id == current.shot_id).values(status="pending"))
        next_id = (current.params or {}).get("chain_next")
        current = await session.get(GenerationTask, int(next_id)) if next_id else None
    await session.commit()
    await _fail_run(session, first.params if first else None, reason)


async def resume_after_cancel(session: AsyncSession, task: GenerationTask) -> None:
    """用户取消排队中的任务后继续推进链，避免后续镜头永远停在 pending。"""
    if task.shot_id:
        await session.execute(update(Shot).where(Shot.id == task.shot_id).values(status="pending"))
    params = task.params or {}
    if params.get("chain_next"):
        await _advance_chain(session, task.id, succeeded=True)
    elif params.get("run_id") and params.get("flow"):
        await _fail_run(session, params, f"任务 {task.id} 已被取消")
    else:
        await session.commit()


_STALE_RUNNING_SECONDS = 3600
_STALE_PENDING_SECONDS = 86400


async def reap_stale_tasks(session: AsyncSession) -> dict[str, int]:
    """回收僵尸任务：worker 重启/崩溃后 running 行会永远卡住，队列丢失的 pending 同理。"""
    now = _now()
    running = list(
        (
            await session.execute(
                select(GenerationTask).where(
                    GenerationTask.status == "running",
                    GenerationTask.started_at.isnot(None),
                    GenerationTask.started_at < now - timedelta(seconds=_STALE_RUNNING_SECONDS),
                )
            )
        ).scalars()
    )
    for task in running:
        task.status = "failed"
        task.finished_at = now
        task.error = "任务超时：worker 可能已重启或外部调用挂起，请重试"
        if task.shot_id:
            await session.execute(update(Shot).where(Shot.id == task.shot_id).values(status="failed"))
    pending = list(
        (
            await session.execute(
                select(GenerationTask).where(
                    GenerationTask.status == "pending",
                    GenerationTask.created_at < now - timedelta(seconds=_STALE_PENDING_SECONDS),
                )
            )
        ).scalars()
    )
    for task in pending:
        task.status = "failed"
        task.finished_at = now
        task.error = "排队超时：任务从未被 worker 执行（队列可能已丢失），请重新发起"
    await session.commit()
    for task in [*running, *pending]:
        await _advance_chain(session, task.id, succeeded=False, error=task.error)
        # 流水线壳任务（kind=pipeline）没有 flow，链推进不会收敛它的运行记录
        if task.kind == "pipeline" and (task.params or {}).get("run_id"):
            await _fail_run(session, task.params, task.error or "流水线任务超时")
    return {"running": len(running), "pending": len(pending)}


async def _finalize(
    session: AsyncSession,
    task_id: int,
    *,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    current = (
        await session.execute(select(GenerationTask.status).where(GenerationTask.id == task_id))
    ).scalar_one_or_none()
    if current == "canceled":
        # 用户已取消，保留取消状态
        return

    values: dict[str, Any] = {"status": status, "finished_at": _now(), "error": error}
    if status == "succeeded":
        values["progress"] = 100
        values["result"] = result
    await session.execute(update(GenerationTask).where(GenerationTask.id == task_id).values(**values))
    await session.commit()
