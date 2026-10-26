"""素材整理 agent：扫描未关联素材 / 重名资产 / 缺参考图 / 分镜里没建档的角色。

刻意不写库：
- 资产合并、补参考图、清理未关联素材都会影响后续生成，必须由人确认；
- 扫描与判定全部是确定性的，不花模型调用；只有需要「排优先级 + 写人话」时才调一次文本模型；
- 结论只进 agent_runs 与响应，不改任何素材 / 资产 / 分镜。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import ProductionAsset
from app.models.episode import Episode
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.shot import Shot
from app.services.agents import runtime
from app.services.agents.audit import record_run
from app.services.providers.base import ProviderError

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是短剧素材库管理员。你只根据扫描结果给整理建议，不执行合并、删除或改写。"
    "每轮只输出一个 JSON：{\"thought\":\"...\",\"action\":\"工具名\",\"args\":{...}}。"
)

_POLICY = (
    "整理策略：\n"
    "1. 先 inspect 读取扫描结果；需要时 rescan 重新扫描（同一份数据，不写库）。\n"
    "2. 用 report 给出：一段人话总结 + 建议处理顺序（按「会直接影响后续生成」优先）。\n"
    "3. 不要编造扫描结果里没有的条目；不要建议直接删除分镜或已认可的关键帧。\n"
    "4. 未关联素材只建议「确认后可清理」，不能当成必须删除。"
)

_KIND_LABELS = {"actor": "角色", "scene": "场景", "prop": "道具", "costume": "服装"}


@dataclass
class _Scan:
    data: dict[str, Any]
    suggestions: list[dict[str, Any]]


@dataclass
class _OrganizeContext:
    summary: str = ""
    order: list[int] | None = None
    deadline: bool = False


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm(name: Any) -> str:
    raw = _text(name).lower()
    return re.sub(r"[\s·・_\-—（）()【】\[\]「」《》]+", "", raw)


def _short(value: Any, limit: int) -> str:
    text = _text(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


async def _scan(session: AsyncSession, *, user_id: int, project_id: int | None) -> _Scan:
    asset_stmt = select(ProductionAsset).where(ProductionAsset.user_id == user_id)
    if project_id is not None:
        asset_stmt = asset_stmt.where(
            or_(
                ProductionAsset.project_id == project_id,
                ProductionAsset.is_global.is_(True),
            )
        )
    assets = list((await session.execute(asset_stmt.order_by(ProductionAsset.id))).scalars())

    shot_stmt = (
        select(Shot)
        .join(Episode, Shot.episode_id == Episode.id)
        .join(Project, Episode.project_id == Project.id)
        .where(Project.user_id == user_id)
    )
    if project_id is not None:
        shot_stmt = shot_stmt.where(Episode.project_id == project_id)
    shots = list((await session.execute(shot_stmt.order_by(Shot.episode_id, Shot.order_index))).scalars())

    media_stmt = select(MediaAsset).where(
        MediaAsset.user_id == user_id, MediaAsset.kind == "image"
    )
    if project_id is not None:
        media_stmt = media_stmt.where(MediaAsset.project_id == project_id)
    images = list((await session.execute(media_stmt.order_by(MediaAsset.id.desc()))).scalars())

    suggestions: list[dict[str, Any]] = []

    # 1) 重名资产：同名同类型多半是重复建档，合并前需人工确认
    groups: dict[tuple[str, str], list[ProductionAsset]] = {}
    for asset in assets:
        key = (_text(asset.kind), _norm(asset.name))
        if not key[1]:
            continue
        groups.setdefault(key, []).append(asset)
    duplicates = [(key, rows) for key, rows in groups.items() if len(rows) > 1]
    for (kind, _name), rows in sorted(duplicates, key=lambda item: -len(item[1]))[:20]:
        suggestions.append(
            {
                "kind": "duplicate_asset",
                "severity": "high",
                "title": f"重名{_KIND_LABELS.get(kind, kind)}：{rows[0].name}",
                "detail": f"共有 {len(rows)} 份同名资产，建议保留一份并把其余资产关联到同一参考图。",
                "target_ids": [int(row.id) for row in rows],
            }
        )

    # 2) 资产缺参考图 / 参考图指向不存在的素材
    media_ids = [int(asset.media_id) for asset in assets if asset.media_id]
    existing_media = set()
    if media_ids:
        existing_media = set(
            (
                await session.execute(
                    select(MediaAsset.id).where(MediaAsset.id.in_(media_ids))
                )
            ).scalars()
        )
    for asset in assets:
        if asset.media_id and int(asset.media_id) not in existing_media:
            suggestions.append(
                {
                    "kind": "missing_media",
                    "severity": "high",
                    "title": f"{_KIND_LABELS.get(_text(asset.kind), asset.kind)}「{asset.name}」参考图丢失",
                    "detail": f"资产 #{asset.id} 指向的素材 {asset.media_id} 已不存在，生成时会拿不到参考图。",
                    "target_ids": [int(asset.id)],
                }
            )
        elif not asset.media_id and _text(asset.kind) in ("actor", "scene"):
            suggestions.append(
                {
                    "kind": "missing_media",
                    "severity": "medium",
                    "title": f"{_KIND_LABELS.get(_text(asset.kind), asset.kind)}「{asset.name}」没有参考图",
                    "detail": f"资产 #{asset.id} 还没有定妆 / 场景参考图，建议先生成参考图再绑定。",
                    "target_ids": [int(asset.id)],
                }
            )
        if _text(asset.kind) in ("actor", "scene") and not _text(asset.prompt):
            suggestions.append(
                {
                    "kind": "missing_prompt",
                    "severity": "medium",
                    "title": f"{_KIND_LABELS.get(_text(asset.kind), asset.kind)}「{asset.name}」缺外观描述",
                    "detail": f"资产 #{asset.id} 没有稳定的外观提示词，跨镜头一致性会退化成模型自由发挥。",
                    "target_ids": [int(asset.id)],
                }
            )

    # 3) 分镜里出现、但素材库没有建档的角色 / 道具
    actor_names = {_norm(asset.name) for asset in assets if _text(asset.kind) == "actor"}
    prop_names = {_norm(asset.name) for asset in assets if _text(asset.kind) == "prop"}
    missing_actors: dict[str, dict[str, Any]] = {}
    missing_props: dict[str, dict[str, Any]] = {}
    for shot in shots:
        for subject in shot.subjects if isinstance(shot.subjects, list) else []:
            if not isinstance(subject, dict):
                continue
            name = _text(subject.get("name"))
            key = _norm(name)
            if not key or key in actor_names:
                continue
            entry = missing_actors.setdefault(key, {"name": name, "shots": []})
            if len(entry["shots"]) < 5:
                entry["shots"].append(int(shot.id))
        for prop in shot.props if isinstance(shot.props, list) else []:
            if not isinstance(prop, dict):
                continue
            name = _text(prop.get("name"))
            key = _norm(name)
            if not key or key in prop_names:
                continue
            entry = missing_props.setdefault(key, {"name": name, "shots": []})
            if len(entry["shots"]) < 5:
                entry["shots"].append(int(shot.id))
    for entry in list(missing_actors.values())[:15]:
        suggestions.append(
            {
                "kind": "unknown_subject",
                "severity": "medium",
                "title": f"角色「{entry['name']}」没有建档",
                "detail": f"{len(entry['shots'])} 个镜头用到这个角色名（如镜头 {entry['shots']}），建议先建角色资产并出定妆照。",
                "target_ids": entry["shots"],
            }
        )
    for entry in list(missing_props.values())[:10]:
        suggestions.append(
            {
                "kind": "unknown_prop",
                "severity": "low",
                "title": f"道具「{entry['name']}」没有建档",
                "detail": f"{len(entry['shots'])} 个镜头用到这个道具名（如镜头 {entry['shots']}）。跨镜头的道具状态容易漂移。",
                "target_ids": entry["shots"],
            }
        )

    # 4) 未关联图片：没有镜头、也没被任何资产引用（只覆盖图片，导出视频不算）
    referenced_media = {int(asset.media_id) for asset in assets if asset.media_id}
    unlinked = [
        media
        for media in images
        if media.shot_id is None and int(media.id) not in referenced_media
    ]
    if unlinked:
        suggestions.append(
            {
                "kind": "unlinked_media",
                "severity": "low",
                "title": f"{len(unlinked)} 张图片素材未关联镜头或资产",
                "detail": "常见来源是旧候选帧、被替换的参考图、手动上传后没用上的图；确认后可清理。",
                "target_ids": [int(media.id) for media in unlinked[:30]],
            }
        )

    by_kind: dict[str, int] = {}
    for asset in assets:
        kind = _text(asset.kind) or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    data = {
        "project_id": project_id,
        "counts": {
            "assets": len(assets),
            "assets_by_kind": by_kind,
            "shots": len(shots),
            "images": len(images),
            "duplicate_groups": len(duplicates),
            "assets_missing_media": sum(1 for asset in assets if not asset.media_id),
            "assets_missing_prompt": sum(
                1
                for asset in assets
                if _text(asset.kind) in ("actor", "scene") and not _text(asset.prompt)
            ),
            "unknown_actor_names": len(missing_actors),
            "unknown_prop_names": len(missing_props),
            "unlinked_images": len(unlinked),
            "suggestions": len(suggestions),
        },
        "unlinked_examples": [
            {
                "id": int(media.id),
                "filename": _short(media.filename, 80),
                "created_at": media.created_at.isoformat(timespec="seconds")
                if media.created_at
                else None,
            }
            for media in unlinked[:10]
        ],
    }
    for index, item in enumerate(suggestions, start=1):
        item["id"] = index
    return _Scan(data=data, suggestions=suggestions)


def _scan_text(scan: _Scan) -> str:
    lines = ["扫描统计："] + [f"- {key}：{value}" for key, value in scan.data["counts"].items()]
    lines.append("候选建议：")
    if not scan.suggestions:
        lines.append("- 没有发现需要整理的问题。")
    for item in scan.suggestions:
        lines.append(
            f"[{item['id']}] ({item['severity']}) {item['title']}：{item['detail']}"
        )
    return "\n".join(lines)


def _apply_order(suggestions: list[dict[str, Any]], order: list[int]) -> list[dict[str, Any]]:
    by_id = {int(item["id"]): item for item in suggestions}
    ordered = [by_id[item] for item in order if item in by_id]
    seen = {int(item["id"]) for item in ordered}
    ordered.extend(item for item in suggestions if int(item["id"]) not in seen)
    return ordered


async def organize_assets(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int | None = None,
    use_llm: bool = True,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """扫描素材库并给出整理建议；默认不写库。"""
    if project_id is not None:
        project = await session.get(Project, int(project_id))
        if project is None or project.user_id != user_id:
            raise ValueError("项目不存在")

    scan = await _scan(session, user_id=user_id, project_id=project_id)
    if not scan.suggestions:
        result = {
            "action": "organize",
            "status": "clean",
            "project_id": project_id,
            "scan": scan.data,
            "suggestions": [],
            "summary": "素材库没有发现需要整理的问题。",
            "steps": 0,
            "trace": [],
            "advisory": True,
        }
        await record_run(
            session,
            user_id=user_id,
            kind="organize",
            status="clean",
            project_id=project_id,
            target_type="project" if project_id else "user",
            target_id=project_id,
            summary={"counts": scan.data["counts"], "summary": result["summary"]},
            trace=[],
        )
        return result

    if not use_llm:
        result = {
            "action": "organize",
            "status": "suggested",
            "project_id": project_id,
            "scan": scan.data,
            "suggestions": scan.suggestions,
            "summary": f"发现 {len(scan.suggestions)} 条可整理项，按严重度排序，未改动任何素材。",
            "steps": 0,
            "trace": [],
            "advisory": True,
        }
        await record_run(
            session,
            user_id=user_id,
            kind="organize",
            status="suggested",
            project_id=project_id,
            target_type="project" if project_id else "user",
            target_id=project_id,
            summary={
                "counts": scan.data["counts"],
                "suggestions": scan.suggestions[:20],
                "summary": result["summary"],
            },
            trace=[],
        )
        return result

    from app.services.generation import _generate_json, build_provider_from, resolve_credential

    try:
        credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    except ProviderError as exc:
        logger.warning("素材整理拿不到文本凭证：%s", exc)
        result = {
            "action": "organize",
            "status": "suggested",
            "project_id": project_id,
            "scan": scan.data,
            "suggestions": scan.suggestions,
            "summary": f"发现 {len(scan.suggestions)} 条可整理项（未使用模型排序：{exc}）。",
            "steps": 0,
            "trace": [],
            "advisory": True,
        }
        await record_run(
            session,
            user_id=user_id,
            kind="organize",
            status="suggested",
            project_id=project_id,
            target_type="project" if project_id else "user",
            target_id=project_id,
            summary={"counts": scan.data["counts"], "suggestions": scan.suggestions[:20]},
            trace=[],
        )
        return result

    provider = build_provider_from(credential)
    decision_model = model or provider.model_for("text")
    ctx = _OrganizeContext()

    async def decide(prompt: str) -> dict[str, Any]:
        return await _generate_json(
            provider,
            prompt,
            system=_SYSTEM,
            model=decision_model,
            temperature=0.2,
            max_tokens=1000,
            attempts=2,
        )

    async def inspect(_: dict[str, Any]) -> runtime.ToolResult:
        return runtime.ToolResult(_scan_text(scan))

    async def rescan(_: dict[str, Any]) -> runtime.ToolResult:
        nonlocal scan
        scan = await _scan(session, user_id=user_id, project_id=project_id)
        return runtime.ToolResult(_scan_text(scan))

    async def report(args: dict[str, Any]) -> runtime.ToolResult:
        summary = _short(args.get("summary"), 800)
        if not summary:
            return runtime.ToolResult("summary 不能为空。", ok=False)
        raw_order = args.get("order")
        order: list[int] = []
        if isinstance(raw_order, str):
            for part in re.split(r"[,，\s]+", raw_order):
                if part.strip().isdigit():
                    order.append(int(part.strip()))
        elif isinstance(raw_order, list):
            order = [int(item) for item in raw_order if str(item).strip().isdigit()]
        ctx.summary = summary
        ctx.order = order or None
        return runtime.ToolResult(
            "已记录整理总结。", data={"summary": summary, "order": ctx.order}, done=True, status="prioritized"
        )

    tools = [
        runtime.AgentTool(
            "inspect", "读取扫描统计与候选建议", {}, inspect, kind="read", max_calls=2
        ),
        runtime.AgentTool(
            "rescan", "按当前范围重新扫描（不写库）", {}, rescan, kind="read", max_calls=1
        ),
        runtime.AgentTool(
            "report",
            "给出总结与处理顺序。必填 summary；可选 order（建议编号，如 \"3,1,2\"）",
            {"summary": "人话总结与注意事项"},
            report,
            kind="write",
            max_calls=1,
        ),
    ]

    run = await runtime.run_agent(
        goal="把素材库扫描结果整理成人能直接执行的建议顺序，不执行任何写操作。",
        tools=tools,
        decide=decide,
        initial_observation=f"{_scan_text(scan)}\n\n{_POLICY}",
        max_steps=4,
        max_expensive_calls=0,
    )

    suggestions = _apply_order(scan.suggestions, ctx.order) if ctx.order else scan.suggestions
    status = "prioritized" if ctx.summary else "suggested"
    if run.status == "agent_error":
        status = "suggested"
    summary = ctx.summary or f"发现 {len(scan.suggestions)} 条可整理项（模型未给出总结）。"
    result = {
        "action": "organize",
        "status": status,
        "project_id": project_id,
        "scan": scan.data,
        "suggestions": suggestions,
        "summary": summary,
        "model": decision_model,
        "steps": run.steps,
        "stop_reason": run.stop_reason,
        "trace": run.trace,
        "advisory": True,
    }
    if run.status == "agent_error":
        result["error"] = (run.final or {}).get("error")
    await record_run(
        session,
        user_id=user_id,
        kind="organize",
        status=status,
        project_id=project_id,
        target_type="project" if project_id else "user",
        target_id=project_id,
        model=decision_model,
        steps=run.steps,
        stop_reason=run.stop_reason,
        summary={
            "counts": scan.data["counts"],
            "summary": summary,
            "order": ctx.order,
            "suggestions": suggestions[:20],
        },
        trace=run.trace,
        error=result.get("error"),
    )
    logger.info(
        "素材整理完成：project=%s status=%s suggestions=%s steps=%s",
        project_id,
        status,
        len(suggestions),
        run.steps,
    )
    return result
