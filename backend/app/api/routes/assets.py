"""生产资产（资产管理）：角色 / 场景 / 道具 / 服装的增删改查与「从素材存为资产」。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.serialize import serialize_assets
from app.db.session import get_db
from app.models.asset import ProductionAsset
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.user import User
from app.schemas.asset import (
    ASSET_KINDS,
    AssetCreate,
    AssetFromMedia,
    AssetGenerateRequest,
    AssetRead,
    AssetUpdate,
)
from app.schemas.task import TaskRead
from app.services.dispatch import create_task
from app.services.references import (
    detach_asset,
    media_of_assets,
    purge_media,
    remove_media_files,
)

router = APIRouter(prefix="/assets", tags=["资产管理"])


def _check_kind(kind: str) -> str:
    if kind not in ASSET_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"资产类型必须是 {' / '.join(ASSET_KINDS)} 之一",
        )
    return kind


async def _validate_media(db: AsyncSession, user_id: int, media_id: int | None) -> None:
    if media_id is None:
        return
    asset = await db.get(MediaAsset, int(media_id))
    if not asset or asset.user_id != user_id or asset.kind != "image":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="参考图素材无效")


async def _validate_project(db: AsyncSession, user_id: int, project_id: int | None) -> None:
    if project_id is None:
        return
    project = await db.get(Project, int(project_id))
    if not project or project.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="项目不存在")


async def _get_owned_asset(db: AsyncSession, user_id: int, asset_id: int) -> ProductionAsset:
    asset = await db.get(ProductionAsset, asset_id)
    if not asset or asset.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资产不存在")
    return asset


@router.get("", response_model=list[AssetRead])
async def list_assets(
    kind: str | None = Query(default=None),
    project_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None),
    include_global: bool = Query(default=True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AssetRead]:
    """列出资产：可按类型 / 项目筛选，也可按名称关键词搜索。

    项目筛选时，同时带出用户勾选了「跨项目复用」的全局资产，方便在任意项目里复用角色。
    """
    stmt = select(ProductionAsset).where(ProductionAsset.user_id == user.id)
    if kind:
        stmt = stmt.where(ProductionAsset.kind == _check_kind(kind))
    if project_id is not None:
        conditions = [ProductionAsset.project_id == project_id]
        if include_global:
            conditions.append(ProductionAsset.is_global.is_(True))
        stmt = stmt.where(or_(*conditions))
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(ProductionAsset.name.ilike(like), ProductionAsset.description.ilike(like))
        )

    rows = (await db.execute(stmt.order_by(ProductionAsset.id.desc()))).scalars().all()
    return await serialize_assets(db, list(rows))


@router.post("", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
async def create_asset(
    payload: AssetCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetRead:
    _check_kind(payload.kind)
    await _validate_media(db, user.id, payload.media_id)
    await _validate_project(db, user.id, payload.project_id)

    asset = ProductionAsset(
        user_id=user.id,
        project_id=payload.project_id,
        kind=payload.kind,
        name=payload.name.strip(),
        description=payload.description,
        prompt=payload.prompt,
        tags=payload.tags,
        attributes=payload.attributes,
        media_id=payload.media_id,
        source="manual",
        is_global=payload.is_global,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return (await serialize_assets(db, [asset]))[0]


@router.post("/from-media", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
async def create_asset_from_media(
    payload: AssetFromMedia,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetRead:
    """把一张已生成的图片存为资产：外观已定，直接复用它的参考图。

    若未填 prompt，则用该图片生成时的提示词作为外观描述，减少手工搬运。
    """
    _check_kind(payload.kind)
    await _validate_media(db, user.id, payload.media_id)
    await _validate_project(db, user.id, payload.project_id)

    media = await db.get(MediaAsset, payload.media_id)
    prompt = payload.prompt
    if not prompt and media:
        meta = media.meta or {}
        # 优先用「干净的主体提示词」，其次才是含资产 / 画质 / 负面块的完整提示词，
        # 因为资产的外观描述要能原样复用，带上一堆修饰段落反而会互相干扰。
        prompt = str(
            meta.get("prompt_clean") or meta.get("prompt") or meta.get("prompt_used") or ""
        ).strip() or None

    asset = ProductionAsset(
        user_id=user.id,
        project_id=payload.project_id or (media.project_id if media else None),
        kind=payload.kind,
        name=payload.name.strip(),
        description=payload.description,
        prompt=prompt,
        media_id=payload.media_id,
        source="generated",
        is_global=payload.is_global,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return (await serialize_assets(db, [asset]))[0]


@router.post("/{asset_id}/generate", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def generate_asset_image(
    asset_id: int,
    payload: AssetGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskRead:
    """按资产的外观描述直接生成一张参考图（角色长什么样 / 场景什么环境）。

    生成完成后由后台任务把这张图挂成该资产的参考图（``production_assets.media_id``），
    之后在分镜生成里关联这个资产，这张图就会作为「图像参考」一起送给模型，
    保证同一个角色 / 场景在各个镜头里长得一样。
    """
    asset = await _get_owned_asset(db, user.id, asset_id)
    # 手动指定的参考图必须是当前用户自己的图片，否则会把别人的素材送进模型
    await _validate_media(db, user.id, payload.reference_media_id)

    # 构图指令由 sheet_prompt_for 统一生成：资产页重画与拆解自动补图走同一份要求，
    # 也顺手剥掉旧提示词里叠着的「三视图」，避免每重画一次多叠一遍
    from app.services.generation import sheet_prompt_for

    auto_prompt, auto_negative = sheet_prompt_for(asset)
    prompt = (payload.prompt or "").strip() or auto_prompt
    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请先填写该资产的外观描述，AI 才能按它出图",
        )

    params = {
        "asset_id": asset.id,
        "prompt": prompt,
        # 标记为资产参考图流程：不套用分镜的起幅/人物站位/镜头质量块，
        # 否则道具出图容易被模型补上一只手甚至一个人
        "flow": "asset_reference",
        "credential_id": payload.credential_id,
        "provider": payload.provider,
        "model": payload.model,
        "negative_prompt": payload.negative_prompt or auto_negative,
        "n": payload.n,
        "aspect_ratio": payload.aspect_ratio,
        "size": payload.size,
        "project_id": payload.project_id or asset.project_id,
        # 换装保脸：默认把资产已有参考图当输入图（图生图），只在既有形象上改服装/造型
        "use_reference": payload.use_reference,
        "reference_media_id": payload.reference_media_id,
    }
    task = await create_task(
        db,
        user_id=user.id,
        kind="image",
        provider=payload.provider,
        model=payload.model,
        project_id=payload.project_id or asset.project_id,
        params=params,
    )
    return TaskRead.model_validate(task)


@router.patch("/{asset_id}", response_model=AssetRead)
async def update_asset(
    asset_id: int,
    payload: AssetUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AssetRead:
    asset = await _get_owned_asset(db, user.id, asset_id)
    values = payload.model_dump(exclude_unset=True)
    if values.get("kind") is not None:
        _check_kind(values["kind"])
    if "media_id" in values:
        await _validate_media(db, user.id, values["media_id"])
    if "project_id" in values:
        await _validate_project(db, user.id, values["project_id"])
    for field, value in values.items():
        setattr(asset, field, value.strip() if isinstance(value, str) and field == "name" else value)
    await db.commit()
    await db.refresh(asset)
    return (await serialize_assets(db, [asset]))[0]


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(
    asset_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """彻底删除资产：解绑分镜引用 + 删资产记录 + 删它专属的参考图（记录与磁盘文件）。

    参考图若还被别的资产 / 关键帧 / 分镜视频引用（例如从素材库某张图建的资产），
    只解引用不删图，避免误删共享素材。
    """
    asset = await _get_owned_asset(db, user.id, asset_id)
    owned = await media_of_assets(db, [asset.id])
    # 先把分镜 subjects / props 上指向它的 asset_id 摘掉（保留描述文字），
    # 否则分镜看着「关联了角色」，实际拿不到参考图，属于静默失败。
    await detach_asset(db, [asset_id])
    await db.delete(asset)
    await db.flush()
    paths = await purge_media(db, owned)
    await db.commit()
    remove_media_files(paths)
