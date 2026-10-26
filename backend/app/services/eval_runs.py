"""金镜头回归，两种口径：

- rejudge：对当前主关键帧跑视觉判官（快、0 生成成本，回答「现在这批画面怎么样」）；
- generate：用冻结编译输入 + 当前编译器真实出图再判（慢、耗额度，回答「改完模板后
  出的图变好还是变差」；产物与主链路隔离，不挂镜头、不写 approved_at）。

金镜头清单来自编译快照 fixtures（与评测第一档同一份冻结资产）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eval_run import EvalRun, EvalRunItem
from app.models.media import MediaAsset
from app.models.shot import Shot
from app.services.generation import load_keyframes, primary_keyframe

logger = logging.getLogger(__name__)

FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "prompt_snapshots"
)


def golden_shot_ids(fixtures_dir: Path | None = None) -> dict[int, list[int]]:
    """从编译快照取金镜头清单：{project_id: [shot_id, …]}，快照不存在则空。"""
    result: dict[int, list[int]] = {}
    directory = fixtures_dir or FIXTURE_DIR
    if not directory.exists():
        return result
    for path in sorted(directory.glob("p*_s*_image.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        project_id = int(data.get("project_id") or 0)
        shot_id = int(data.get("shot_id") or 0)
        if project_id and shot_id and shot_id not in result.setdefault(project_id, []):
            result[project_id].append(shot_id)
    return result


MODE_REJUDGE = "rejudge"
MODE_GENERATE = "generate"
_KIND_BY_MODE = {MODE_REJUDGE: "golden_shot", MODE_GENERATE: "golden_generate"}


def snapshot_for(project_id: int, shot_id: int) -> dict | None:
    path = FIXTURE_DIR / f"p{project_id}_s{shot_id}_image.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _generate_candidate(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    shot: Shot,
    snapshot: dict,
    eval_run_id: int,
):
    """用冻结输入 + 当前编译器真实出一张图，落成隔离的评测素材（不挂镜头/关键帧）。"""
    from app.services import prompts, storage
    from app.services.generation import (
        build_provider_from,
        load_keyframes,
        primary_keyframe,
        resolve_credential,
    )

    inputs = dict(snapshot["inputs"])
    # 冻结输入只带参考图身份（图例用），真正送模型的 URL 从上一张生产关键帧取：
    # 这样「改模板后重出」和「生产当时那张」用的是同一批参考图，对比才公平。
    keyframes = (await load_keyframes(session, [shot.id])).get(shot.id, [])
    primary = primary_keyframe(keyframes)
    reference_urls: list[str] = []
    reference_roles: list[dict] = []
    if primary is not None and primary.media_id:
        source = await session.get(MediaAsset, int(primary.media_id))
        meta = (source.meta or {}) if source else {}
        reference_urls = [str(url) for url in (meta.get("reference_images") or []) if url][:4]
        reference_roles = list(meta.get("reference_roles") or [])
    if reference_roles:
        inputs["references"] = [
            {
                "kind": str(role.get("kind") or "actor"),
                "name": str(role.get("name") or ""),
                "url": None,
            }
            for role in reference_roles
            if role.get("name")
        ][:4]

    prompt, _ = prompts.compose_generation_prompt(**inputs)
    credential = await resolve_credential(session, user_id, "image")
    provider = build_provider_from(credential)
    result = await provider.generate_image(
        prompt,
        aspect_ratio=inputs.get("aspect_ratio"),
        reference_image_urls=reference_urls or None,
    )
    asset = await storage.persist_media(
        session,
        user_id=user_id,
        kind="image",
        provider=credential.provider,
        model=(result.meta or {}).get("model"),
        # 不挂项目/镜头：评测素材不能混进项目素材库，更不能被当成关键帧
        project_id=None,
        shot_id=None,
        task_id=None,
        url=result.url,
        data=result.data,
        mime_type=result.mime_type,
        meta={
            **(result.meta or {}),
            "eval_isolated": True,
            "eval_run_id": eval_run_id,
            "golden_project_id": project_id,
            "golden_shot_id": shot.id,
            "prompt_used": prompt,
            "reference_images": reference_urls,
            "reference_roles": reference_roles,
        },
    )
    await session.commit()
    return asset


async def run_golden_regression(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    mode: str = MODE_REJUDGE,
) -> dict:
    """跑一遍金镜头回归，与同类上一次 run 的结论对比。

    mode=rejudge 审查当前主关键帧；mode=generate 按当前编译器真实出图再审查。
    """
    from app.services.agents.visual_qa import review_keyframe
    from app.services.prompts import compiler_version

    if mode not in _KIND_BY_MODE:
        raise ValueError(f"未知回归模式：{mode}")
    kind = _KIND_BY_MODE[mode]
    run = EvalRun(
        user_id=user_id,
        project_id=project_id,
        kind=kind,
        status="running",
        prompt_version=compiler_version(),
    )
    session.add(run)
    await session.flush()

    prev_items = await _previous_verdicts(session, project_id, kind)
    shot_ids = golden_shot_ids().get(project_id, [])
    if not shot_ids:
        run.status = "failed"
        run.error = "本机没有该项目金镜头快照，先跑 export_eval_snapshots.py 导出"
        await session.commit()
        return {"run_id": run.id, "status": run.status, "error": run.error}

    totals: dict[str, int] = {"pass": 0, "warn": 0, "fail": 0, "error": 0, "skipped": 0}
    items: list[EvalRunItem] = []
    for shot_id in shot_ids:
        item = EvalRunItem(
            run_id=run.id, shot_id=shot_id, prev_verdict=prev_items.get(shot_id)
        )
        try:
            shot = await session.get(Shot, shot_id)
            keyframes = (await load_keyframes(session, [shot_id])).get(shot_id, [])
            primary = primary_keyframe(keyframes)
            asset = None
            if mode == MODE_GENERATE and shot is not None:
                snapshot = snapshot_for(project_id, shot_id)
                if snapshot is None:
                    item.error = "没有该镜头的冻结编译输入"
                    totals["skipped"] += 1
                else:
                    asset = await _generate_candidate(
                        session,
                        user_id=user_id,
                        project_id=project_id,
                        shot=shot,
                        snapshot=snapshot,
                        eval_run_id=run.id,
                    )
                    item.media_id = int(asset.id)
            elif shot is not None and primary is not None and primary.media_id:
                item.media_id = int(primary.media_id)
                asset = await session.get(MediaAsset, int(primary.media_id))
            else:
                item.error = "没有主关键帧可审"
                totals["skipped"] += 1

            if asset is not None and shot is not None:
                review = await review_keyframe(session, user_id=user_id, asset=asset, shot=shot)
                item.verdict = review.get("verdict")
                item.checks = review.get("checks")
                item.reasons = review.get("reasons")
                totals[review.get("verdict") or "error"] = (
                    totals.get(review.get("verdict") or "error", 0) + 1
                )
                item.changed = (
                    item.prev_verdict is not None and item.prev_verdict != item.verdict
                )
        except Exception as exc:  # noqa: BLE001 - 单镜失败不拖垮整轮
            item.error = f"{type(exc).__name__}: {exc}"[:500]
            totals["error"] += 1
            logger.warning("金镜头 %s 回归审查失败：%s", shot_id, exc)
            # rollback 会把 run 和此前所有已 flush 的 items 一并回滚，全部重新挂回
            await session.rollback()
            session.add(run)
            for kept in items:
                session.add(kept)
        items.append(item)
        session.add(item)
        await session.flush()

    run.status = "completed"
    run.totals = totals
    await session.commit()
    return {"run_id": run.id, "status": run.status, "mode": mode, "totals": totals}


async def _previous_verdicts(
    session: AsyncSession, project_id: int, kind: str = "golden_shot"
) -> dict[int, str | None]:
    """上一次已完成 run 里各镜头的结论，用于「变好 / 变差」对比。

    只在同一口径内比较：审查旧图不能和「重新出图」的结论混在一起算变化。
    """
    run = (
        await session.execute(
            select(EvalRun)
            .where(
                EvalRun.project_id == project_id,
                EvalRun.kind == kind,
                EvalRun.status == "completed",
            )
            .order_by(EvalRun.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if run is None:
        return {}
    items = (
        await session.execute(select(EvalRunItem).where(EvalRunItem.run_id == run.id))
    ).scalars().all()
    return {int(item.shot_id): item.verdict for item in items if item.shot_id}
