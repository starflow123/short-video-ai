"""角色 / 道具资产沉淀：把分镜里的姓名绑定到可复用的生产资产。

分镜 JSON 只写姓名，生成时却需要稳定的外观描述与参考图。这里负责：
- 从角色档案创建 / 更新「演员」资产；
- 把镜头 subjects / props 里的姓名解析成 asset_id；
- 为反复出现的道具补建「道具」资产（先有文字描述，参考图可后续生成）。
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import ProductionAsset
from app.services import prompts


def _same_name(left: object, right: object) -> bool:
    a = str(left or "").strip()
    b = str(right or "").strip()
    return bool(a and b and (a == b or a in b or b in a))


def _match_asset(assets: list[ProductionAsset], name: object) -> ProductionAsset | None:
    """先按姓名完全相等，再退回包含匹配：避免「杨间」先撞上「杨间（成神形态）」。"""
    target = str(name or "").strip()
    if not target:
        return None
    exact = next((item for item in assets if str(item.name or "").strip() == target), None)
    if exact is not None:
        return exact
    return next((item for item in assets if _same_name(item.name, target)), None)


def _actor_look(subject: dict) -> str:
    """镜头里写的角色外观 → 演员资产的初始外观描述（定妆照提示词只用这一段）。"""
    parts = [
        str(subject.get("appearance") or "").strip(),
        str(subject.get("costume") or "").strip(),
    ]
    return "；".join(part for part in parts if part)


def profile_from_asset(asset: ProductionAsset) -> dict:
    """把演员资产还原成角色档案结构，供分镜提示词复用。"""
    attributes = asset.attributes or {}
    return {
        "name": asset.name,
        "age": attributes.get("age") or "",
        "role": attributes.get("role") or "",
        "appearance": attributes.get("appearance") or "",
        "costume": attributes.get("costume") or "",
        "traits": asset.description or "",
        "tags": list(asset.tags or []),
        "asset_id": asset.id,
    }


async def load_project_characters(
    session: AsyncSession, *, user_id: int, project_id: int
) -> list[dict]:
    rows = (
        await session.execute(
            select(ProductionAsset)
            .where(
                ProductionAsset.user_id == user_id,
                ProductionAsset.project_id == project_id,
                ProductionAsset.kind == "actor",
            )
            .order_by(ProductionAsset.id)
        )
    ).scalars().all()
    return [profile_from_asset(item) for item in rows]


async def load_assets_without_reference(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    kinds: tuple[str, ...] = ("actor", "prop"),
) -> list[ProductionAsset]:
    """取本项目「还没有参考图」的角色 / 道具资产，供自动生成定妆照。"""
    rows = (
        await session.execute(
            select(ProductionAsset).where(
                ProductionAsset.user_id == user_id,
                ProductionAsset.project_id == project_id,
                ProductionAsset.kind.in_(kinds),
                ProductionAsset.media_id.is_(None),
            )
        )
    ).scalars().all()
    return [item for item in rows if (item.prompt or item.description)]


async def load_shared_actors(
    session: AsyncSession, *, user_id: int
) -> list[ProductionAsset]:
    """资产库里可跨项目复用的演员：全局角色 + 还没归到任何项目的手工角色。

    这些形象已经出过定妆照，分镜只要按姓名挂上就能拿到同一张脸；
    它们不随项目删除，所以只读不写（不擅自改 project_id）。
    """
    rows = (
        await session.execute(
            select(ProductionAsset).where(
                ProductionAsset.user_id == user_id,
                ProductionAsset.kind == "actor",
                or_(
                    ProductionAsset.project_id.is_(None),
                    ProductionAsset.is_global.is_(True),
                ),
            )
        )
    ).scalars().all()
    return list(rows)


async def ensure_character_assets(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    profiles: list[dict],
    update_existing: bool = True,
) -> list[dict]:
    """角色档案 → 演员资产（同名复用并更新外观，避免每次 forge 都重复建档）。

    ``update_existing=False`` 用于拆解链路：只补缺失的角色，不覆盖用户手改过的外观。
    """
    existing = (
        await session.execute(
            select(ProductionAsset).where(
                ProductionAsset.user_id == user_id,
                ProductionAsset.project_id == project_id,
                ProductionAsset.kind == "actor",
            )
        )
    ).scalars().all()
    by_name = {item.name: item for item in existing}
    result: list[dict] = []

    for profile in profiles:
        name = str(profile.get("name") or "").strip()
        if not name:
            continue
        look = prompts.character_asset_prompt(profile)
        asset = by_name.get(name)
        if asset is None:
            asset = ProductionAsset(
                user_id=user_id,
                project_id=project_id,
                kind="actor",
                name=name,
                description=profile.get("traits") or None,
                prompt=look or None,
                tags=profile.get("tags") or None,
                attributes={
                    "age": profile.get("age") or "",
                    "role": profile.get("role") or "",
                    "appearance": profile.get("appearance") or "",
                    "costume": profile.get("costume") or "",
                },
                source="generated",
                is_global=False,
            )
            session.add(asset)
            by_name[name] = asset
        else:
            if update_existing:
                if look:
                    asset.prompt = look
                if profile.get("traits"):
                    asset.description = profile["traits"]
                if profile.get("tags"):
                    asset.tags = profile["tags"]
                asset.attributes = {
                    **(asset.attributes or {}),
                    "age": profile.get("age") or "",
                    "role": profile.get("role") or "",
                    "appearance": profile.get("appearance") or "",
                    "costume": profile.get("costume") or "",
                }
            else:
                # 只补空字段：拆解链路不能覆盖用户手改过的外观
                if look and not asset.prompt:
                    asset.prompt = look
                if profile.get("traits") and not asset.description:
                    asset.description = profile["traits"]
                attributes = dict(asset.attributes or {})
                for key in ("age", "role", "appearance", "costume"):
                    if not str(attributes.get(key) or "").strip() and profile.get(key):
                        attributes[key] = profile[key]
                asset.attributes = attributes
        await session.flush()
        result.append({**profile, "name": name, "asset_id": asset.id})

    return result


def _prop_prompt(prop: dict) -> str:
    parts = [str(prop.get("description") or "").strip(), str(prop.get("state") or "").strip()]
    text = "，".join(part for part in parts if part)
    return f"{prop['name']}：{text}" if text else str(prop["name"])


async def bind_shot_assets(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    shots: list,
) -> dict[str, int]:
    """把镜头的 subjects / props 姓名解析成资产 id，并补齐外观字段。

    - 角色：本项目同名演员优先；其次复用资产库里已出过定妆照的全局 / 无归属角色；
      都找不到就按镜头里写的外观新建，保证每个出场人物都有资产可挂参考图；
    - 道具：同名道具资产复用，没有则新建（仅文字，后续可生成参考图）；
    - 已有 asset_id 的不覆盖，尊重人工指定。
    """
    assets = (
        await session.execute(
            select(ProductionAsset).where(
                ProductionAsset.user_id == user_id,
                ProductionAsset.project_id == project_id,
                ProductionAsset.kind.in_(("actor", "prop")),
            )
        )
    ).scalars().all()
    actors = [item for item in assets if item.kind == "actor"]
    props = [item for item in assets if item.kind == "prop"]
    props_by_name = {item.name: item for item in props}
    shared_actors = await load_shared_actors(session, user_id=user_id)

    bound_actors = 0
    bound_props = 0
    created_actors = 0
    created_props = 0

    for shot in shots:
        subjects = [dict(item) for item in (shot.subjects or []) if isinstance(item, dict)]
        prop_items = [dict(item) for item in (shot.props or []) if isinstance(item, dict)]

        for subject in subjects:
            if subject.get("asset_id"):
                continue
            name = str(subject.get("name") or "").strip()
            if not name:
                continue
            project_match = _match_asset(actors, name)
            shared_match = _match_asset(shared_actors, name)
            # 有定妆照的形象优先：库里已有这张脸时，不能被同名空档案顶掉
            asset = (
                project_match
                if project_match is not None and project_match.media_id
                else shared_match
                if shared_match is not None and shared_match.media_id
                else project_match or shared_match
            )
            if asset is None:
                # 直接拆解（没走 forge）时角色档案不存在：先建一个，定妆照随后补上，
                # 缺了这一步关键帧就没有人物参考图，每个镜头都会换一张脸。
                asset = ProductionAsset(
                    user_id=user_id,
                    project_id=project_id,
                    kind="actor",
                    name=name,
                    prompt=_actor_look(subject) or None,
                    attributes={
                        "appearance": subject.get("appearance") or "",
                        "costume": subject.get("costume") or "",
                    },
                    source="generated",
                    is_global=False,
                )
                session.add(asset)
                actors.append(asset)
                created_actors += 1
                await session.flush()
            subject["asset_id"] = asset.id
            bound_actors += 1
            attributes = asset.attributes or {}
            if not subject.get("appearance") and attributes.get("appearance"):
                subject["appearance"] = attributes["appearance"]
            if not subject.get("costume") and attributes.get("costume"):
                subject["costume"] = attributes["costume"]

        for prop in prop_items:
            if prop.get("asset_id"):
                continue
            name = str(prop.get("name") or "").strip()
            if not name:
                continue
            asset = props_by_name.get(name) or next(
                (item for item in props if _same_name(item.name, name)), None
            )
            if asset is None:
                asset = ProductionAsset(
                    user_id=user_id,
                    project_id=project_id,
                    kind="prop",
                    name=name,
                    description=prop.get("description") or prop.get("state") or None,
                    prompt=_prop_prompt(prop),
                    attributes={"state": prop.get("state") or ""},
                    source="generated",
                    is_global=False,
                )
                session.add(asset)
                props.append(asset)
                props_by_name[name] = asset
                created_props += 1
                await session.flush()
            prop["asset_id"] = asset.id
            bound_props += 1

        shot.subjects = subjects or None
        shot.props = prop_items or None

    await session.flush()
    return {
        "actors": bound_actors,
        "props": bound_props,
        "new_actors": created_actors,
        "new_props": created_props,
    }
