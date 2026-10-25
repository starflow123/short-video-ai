"""删除资源时清理关联引用。

`media_assets.id` 与 `production_assets.id` 在多个表里是以裸整数保存的（有意不用外键，
避免循环依赖），所以数据库层没有级联可言——不清理就会留下悬空引用：分镜指向已删除的素材、
关键帧指向不存在的图，生成时静默拿不到参考图 / 首帧，用户却看不出原因。
这里把这些「业务层保证一致性」的责任集中实现，供各删除入口调用。
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import ProductionAsset
from app.models.episode import Episode
from app.models.keyframe import ShotKeyframe
from app.models.media import MediaAsset
from app.models.shot import Shot

logger = logging.getLogger(__name__)


async def detach_media(db: AsyncSession, media_ids: list[int]) -> dict[str, int]:
    """素材被删除后，清掉所有指向它的引用。

    - 关键帧记录整行删除：这张记录除了 media_id 没有别的信息，留着就是空壳；
    - 分镜的视频引用置空，并把「已完成」状态按实际情况回退（视频没了就不算完成）；
    - 资产的参考图置空：资产记录保留，可稍后重新生成参考图。
    """
    ids = sorted({int(item) for item in media_ids if item is not None})
    if not ids:
        return {"keyframes_deleted": 0, "shots_reset": 0, "assets_detached": 0}

    # 先把需要的数据全部取出来，避免删除后 ORM 实例过期触发懒加载
    media_rows = (
        await db.execute(select(MediaAsset).where(MediaAsset.id.in_(ids)))
    ).scalars().all()
    shot_ids = {row.shot_id for row in media_rows if row.shot_id}

    keyframes = (
        await db.execute(select(ShotKeyframe).where(ShotKeyframe.media_id.in_(ids)))
    ).scalars().all()
    for row in keyframes:
        shot_ids.add(row.shot_id)
        await db.delete(row)

    videos = (
        await db.execute(select(Shot).where(Shot.video_media_id.in_(ids)))
    ).scalars().all()
    for shot in videos:
        shot.video_media_id = None
        shot_ids.add(shot.id)

    assets = (
        await db.execute(select(ProductionAsset).where(ProductionAsset.media_id.in_(ids)))
    ).scalars().all()
    for asset in assets:
        asset.media_id = None

    await db.flush()

    # 重算受影响镜头的状态：关键帧 / 视频都没了，就不能再标成已完成
    reset = 0
    if shot_ids:
        shots = (await db.execute(select(Shot).where(Shot.id.in_(shot_ids)))).scalars().all()
        for shot in shots:
            if shot.status not in ("ready", "prepared"):
                continue
            has_keyframe = (
                await db.execute(select(ShotKeyframe.id).where(ShotKeyframe.shot_id == shot.id).limit(1))
            ).scalar() is not None
            if shot.video_media_id:
                continue
            shot.status = "prepared" if has_keyframe else "pending"
            reset += 1

    await db.flush()
    result = {
        "keyframes_deleted": len(keyframes),
        "shots_reset": reset,
        "assets_detached": len(assets),
    }
    logger.info("素材级联清理 %s -> %s", ids, result)
    return result


async def detach_asset(db: AsyncSession, asset_ids: list[int]) -> dict[str, int]:
    """资产被删除后，把分镜里 subjects / props 上指向它的 asset_id 摘掉。

    只删引用、不删描述文字：提示词里「谁·站位·动作·拿着什么」原样保留，
    但不会再指着一个不存在的资产；前端会显示「未绑定资产」，用户可重新关联。
    这样避免「看着关联了资产、其实拿不到参考图」的静默失败。
    """
    ids = {int(item) for item in asset_ids if item is not None}
    if not ids:
        return {"shots_cleaned": 0, "refs_removed": 0}

    shots = (
        await db.execute(
            select(Shot).where((Shot.subjects.is_not(None)) | (Shot.props.is_not(None)))
        )
    ).scalars().all()

    cleaned = 0
    removed = 0
    for shot in shots:
        changed = False
        for field in ("subjects", "props"):
            rows = getattr(shot, field)
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows:
                if isinstance(row, dict) and row.get("asset_id") in ids:
                    row = {key: value for key, value in row.items() if key != "asset_id"}
                    removed += 1
                    changed = True
                kept.append(row)
            setattr(shot, field, kept)
        if changed:
            cleaned += 1

    await db.flush()
    result = {"shots_cleaned": cleaned, "refs_removed": removed}
    logger.info("资产级联清理 %s -> %s", sorted(ids), result)
    return result


async def detach_task(db: AsyncSession, task_ids: list[int]) -> dict[str, int]:
    """任务记录被删除后：解除素材对它的引用（素材本身保留在素材库）。"""
    ids = sorted({int(item) for item in task_ids if item is not None})
    if not ids:
        return {"media_unlinked": 0}
    rows = (
        await db.execute(select(MediaAsset).where(MediaAsset.task_id.in_(ids)))
    ).scalars().all()
    for row in rows:
        row.task_id = None
    await db.flush()
    logger.info("任务级联清理 %s -> 解除素材引用 %s", ids, len(rows))
    return {"media_unlinked": len(rows)}


# ---------------------------------------------------------------- 彻底删除
async def media_of_shots(db: AsyncSession, shot_ids: list[int]) -> list[int]:
    """这些分镜「名下」的素材：各关键帧的图 + 分镜视频。删分镜前先取，删完才能判断谁还在用。"""
    ids = {int(item) for item in shot_ids if item is not None}
    if not ids:
        return []
    found = (
        await db.execute(select(ShotKeyframe.media_id).where(ShotKeyframe.shot_id.in_(ids)))
    ).scalars().all()
    videos = (
        await db.execute(select(Shot.video_media_id).where(Shot.id.in_(ids)))
    ).scalars().all()
    return sorted({int(item) for item in [*found, *videos] if item is not None})


async def media_of_assets(db: AsyncSession, asset_ids: list[int]) -> list[int]:
    ids = {int(item) for item in asset_ids if item is not None}
    if not ids:
        return []
    found = (
        await db.execute(select(ProductionAsset.media_id).where(ProductionAsset.id.in_(ids)))
    ).scalars().all()
    return sorted({int(item) for item in found if item is not None})


async def shots_of_episodes(db: AsyncSession, episode_ids: list[int]) -> list[int]:
    ids = {int(item) for item in episode_ids if item is not None}
    if not ids:
        return []
    found = (await db.execute(select(Shot.id).where(Shot.episode_id.in_(ids)))).scalars().all()
    return [int(item) for item in found]


async def shots_of_projects(db: AsyncSession, project_ids: list[int]) -> list[int]:
    ids = {int(item) for item in project_ids if item is not None}
    if not ids:
        return []
    found = (
        await db.execute(
            select(Shot.id).join(Episode, Shot.episode_id == Episode.id).where(
                Episode.project_id.in_(ids)
            )
        )
    ).scalars().all()
    return [int(item) for item in found]


async def project_media_ids(db: AsyncSession, project_ids: list[int]) -> list[int]:
    """直接挂在项目名下的素材（上传图、成片等）。"""
    ids = {int(item) for item in project_ids if item is not None}
    if not ids:
        return []
    found = (
        await db.execute(select(MediaAsset.id).where(MediaAsset.project_id.in_(ids)))
    ).scalars().all()
    return [int(item) for item in found]


async def assets_of_projects(db: AsyncSession, project_ids: list[int]) -> list[int]:
    ids = {int(item) for item in project_ids if item is not None}
    if not ids:
        return []
    found = (
        await db.execute(
            select(ProductionAsset.id).where(
                ProductionAsset.project_id.in_(ids), ProductionAsset.is_global.is_(False)
            )
        )
    ).scalars().all()
    return [int(item) for item in found]


async def media_paths_of(db: AsyncSession, media_ids: list[int]) -> dict[int, Path]:
    """在删除宿主之前先把素材的磁盘路径读出来。

    ``media_assets.project_id`` 是 ``ON DELETE CASCADE``：删项目时数据库会先把素材行连带删掉，
    之后 ``purge_media`` 再查就什么都查不到，路径也就拿不到，文件全留在磁盘上。
    """
    ids = sorted({int(item) for item in media_ids if item is not None})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(MediaAsset.id, MediaAsset.path).where(MediaAsset.id.in_(ids))
        )
    ).all()
    return {int(media_id): Path(path) for media_id, path in rows}


async def purge_media(
    db: AsyncSession,
    media_ids: list[int],
    *,
    preloaded: dict[int, Path] | None = None,
) -> list[Path]:
    """彻底删除素材：删记录并返回待删的磁盘路径。

    只删「此刻已经没有任何引用」的素材——资产参考图、关键帧、分镜视频都清完之后再看，
    仍被别人引用的（例如从某张素材建出来的全局资产）保留，避免误删共享图。
    必须在删除宿主记录并 flush 之后调用，否则引用还挂着，什么都删不掉。
    ``preloaded`` 是宿主删除前抢读的路径快照，用于兜住数据库级联已经删掉的行。
    """
    ids = sorted({int(item) for item in media_ids if item is not None})
    if not ids:
        return []
    referenced: set[int] = set()
    for column in (ProductionAsset.media_id, ShotKeyframe.media_id, Shot.video_media_id):
        rows = (await db.execute(select(column).where(column.in_(ids)))).scalars().all()
        referenced.update(int(item) for item in rows if item is not None)
    dead = [item for item in ids if item not in referenced]
    if not dead:
        return []
    rows = (
        await db.execute(
            select(MediaAsset.id, MediaAsset.path).where(MediaAsset.id.in_(dead))
        )
    ).all()
    paths = [Path(path) for _, path in rows]
    for media_id, _ in rows:
        record = await db.get(MediaAsset, int(media_id))
        if record is not None:
            await db.delete(record)
    paths += [
        path for media_id, path in (preloaded or {}).items()
        if media_id in set(dead) and media_id not in {int(item) for item, _ in rows}
    ]
    await db.flush()
    logger.info("彻底删除素材 %s（跳过仍被引用的 %s）", dead, sorted(set(ids) - set(dead)))
    return paths


def remove_media_files(paths: list[Path]) -> None:
    """删记录之后再删文件；单个文件删不掉不影响主流程（下次删除会再试）。"""
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:  # noqa: BLE001
            logger.warning("素材文件删除失败：%s", path)
