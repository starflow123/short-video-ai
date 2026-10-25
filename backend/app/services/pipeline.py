"""制作流水线：用 LangGraph 编排「建角色档案 → AI 出分镜 → 人工确认 → 批量生图」。

设计取舍
--------
- **LangGraph 负责高层编排，Celery 负责实际生成**。图里只做「该跑哪一步、状态怎么流转」，
  真正的 LLM 调用、图片 / 视频渲染仍交给现有的 Celery 任务（``app.services.dispatch``），
  避免把长耗时、可重试、带进度回写的工作塞进图节点。
- **人工确认是一个真实的中断点**：图跑到 ``generate_breakdown`` 就结束（``END``），
  分镜全部落库且 ``shot.prompt_confirmed=False``、``pipeline_run.status="reviewing"``。
  用户在分镜页逐个审阅 / 修改提示词，确认后另一次调用把图从 ``batch_generate_images``
  入口重新跑一遍。这样「暂停等人」不需要进程常驻，也不需要把图执行器做 checkpoint 持久化。
- **只重跑某个镜头**：不走流水线，直接调用现有的 ``POST /shots/{id}/image``。

阶段流转
--------
``start``       → build_characters → generate_breakdown →（暂停，status=reviewing）
``confirmed``   → batch_generate_images → END（status=generating）
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.episode import Episode
from app.models.keyframe import ShotKeyframe
from app.models.pipeline import PipelineRun
from app.models.project import Project
from app.models.shot import Shot
from app.services import assets as asset_service
from app.services import prompts
from app.services.providers.base import ProviderError

logger = logging.getLogger(__name__)


class PipelineUnavailable(RuntimeError):
    """LangGraph 未安装时的明确提示（避免静默失败）。"""


# ---------------------------------------------------------------- 状态
class PipelineState(TypedDict, total=False):
    run_id: int
    episode_id: int
    stage: str  # start / confirmed
    characters: list[dict]
    shot_count: int
    generated: int
    error: str


# ---------------------------------------------------------------- 图节点
async def _build_characters(state: PipelineState) -> dict[str, Any]:
    """第一步：从剧本提炼角色档案（长什么样 / 穿什么衣服），并落库为演员资产。"""
    # 懒加载：generation 需要 import 本模块以分发流水线任务，模块级互相 import 会成环
    from app.services.generation import (
        _project_style_text,
        build_provider_from,
        resolve_credential,
    )

    async with AsyncSessionLocal() as session:
        run = await session.get(PipelineRun, int(state["run_id"]))
        if not run:
            raise ProviderError("流水线记录不存在")
        params = run.params or {}
        episode = await session.get(Episode, int(state["episode_id"]))
        if not episode:
            raise ProviderError("分集不存在")
        project = await session.get(Project, episode.project_id)
        script = (episode.script_text or "").strip()
        if not script:
            raise ProviderError("分集剧本为空，请先填写剧本再启动流水线")

        # 关闭「重新抽取」时复用该分集上一次的角色档案，省掉一次 LLM 调用
        if params.get("rebuild_characters") is False:
            previous = (
                await session.execute(
                    select(PipelineRun)
                    .where(
                        PipelineRun.episode_id == episode.id,
                        PipelineRun.id != run.id,
                        PipelineRun.characters.isnot(None),
                    )
                    .order_by(PipelineRun.id.desc())
                    .limit(1)
                )
            ).scalars().first()
            if previous and previous.characters:
                run.characters = previous.characters
                run.status = "running"
                run.stage = "breakdown"
                await session.commit()
                return {"characters": previous.characters}

        credential = await resolve_credential(
            session, run.user_id, "text", params.get("credential_id"), params.get("provider")
        )
        provider = build_provider_from(credential)
        raw = await _json_call(
            provider,
            prompts.build_character_profile_prompt(
                script=script,
                style=params.get("style") or _project_style_text(project),
                aspect_ratio=project.aspect_ratio if project else "9:16",
            ),
            system=prompts.CHARACTER_PROFILE_SYSTEM,
            model=params.get("model"),
            temperature=0.6,
            max_tokens=4096,
        )
        profiles = prompts.parse_character_profiles(raw)

        # 角色档案同时落库为「演员」资产：后续任意镜头的提示词都能一键关联复用，保证同一个人
        characters = await asset_service.ensure_character_assets(
            session,
            user_id=run.user_id,
            project_id=episode.project_id,
            profiles=profiles,
        )

        run.characters = characters
        run.status = "running"
        run.stage = "breakdown"
        await session.commit()

        # 【角色定妆照】档案落库后立刻给还没有参考图的角色 / 道具各出一张标准参考图，
        # 作为后续分镜生成的默认图像参考；串行生成避免挤爆图片厂商并发队列。
        from app.services.generation import _spawn_asset_reference_tasks

        sheet_tasks = await _spawn_asset_reference_tasks(
            session,
            user_id=run.user_id,
            project_id=episode.project_id,
            assets=await asset_service.load_assets_without_reference(
                session, user_id=run.user_id, project_id=episode.project_id
            ),
            aspect_ratio=project.aspect_ratio if project else None,
        )
        if sheet_tasks:
            await _start_chain(session, sheet_tasks)
        return {"characters": characters}


async def _generate_breakdown(state: PipelineState) -> dict[str, Any]:
    """第二步：带着角色档案出分镜；跑完即结束（等人工确认，不自动生图）。"""
    # 懒加载：避免与 generation 形成模块级循环依赖（generation 需要 import 本模块分发流水线任务）
    from app.services.generation import (
        _apply_breakdown,
        _breakdown_payload,
        _project_style_text,
        build_provider_from,
        resolve_credential,
    )

    async with AsyncSessionLocal() as session:
        run = await session.get(PipelineRun, int(state["run_id"]))
        if not run:
            raise ProviderError("流水线记录不存在")
        params = run.params or {}
        episode = await session.get(Episode, int(state["episode_id"]))
        if not episode:
            raise ProviderError("分集不存在")
        project = await session.get(Project, episode.project_id)
        script = (episode.script_text or "").strip()
        if not script:
            raise ProviderError("分集剧本为空，请先填写剧本")

        credential = await resolve_credential(
            session, run.user_id, "text", params.get("credential_id"), params.get("provider")
        )
        provider = build_provider_from(credential)
        # 角色档案以「姓名 + 外观 + 服装」的形式注入，让每个镜头的提示词都锁定同一批人
        characters = [
            {
                "name": item.get("name"),
                "appearance": item.get("appearance"),
                "costume": item.get("costume"),
                "role": item.get("role"),
            }
            for item in (state.get("characters") or run.characters or [])
        ]
        confirmed_plan = prompts.normalize_plan(params["plan"]) if params.get("plan") else None
        raw_count = params.get("shot_count")
        shot_count = max(1, min(60, int(raw_count))) if raw_count else None
        if not shot_count and confirmed_plan:
            planned = sum(int(beat.get("shots") or 0) for beat in confirmed_plan["beats"])
            shot_count = planned if 1 <= planned <= 60 else None
        target_seconds = int(params["target_seconds"]) if params.get("target_seconds") else None
        payload = await _breakdown_payload(
            provider,
            model=params.get("model"),
            title=episode.title,
            script=script,
            style=params.get("style") or _project_style_text(project),
            aspect_ratio=project.aspect_ratio if project else "9:16",
            shot_count=shot_count,
            target_seconds=target_seconds,
            characters=characters,
            skills=params.get("skills"),
            confirmed_plan=confirmed_plan,
        )
        shots = await _apply_breakdown(session, episode, payload, confirmed_plan=confirmed_plan)
        # 把分镜里的姓名绑定到演员 / 道具资产：生成时才能按「本镜头出场角色」挑参考图
        await asset_service.bind_shot_assets(
            session, user_id=run.user_id, project_id=episode.project_id, shots=shots
        )
        # 全部置为「待人工确认」：批量生图只认已确认的镜头
        for shot in shots:
            shot.prompt_confirmed = False
        episode.status = "reviewing"

        run.status = "reviewing"
        run.stage = "review"
        await session.commit()

        # 道具资产是在拆解分镜时才落库的，角色定妆照那一步还不存在；这里补一道
        # 「单件物品」参考图，否则道具特写只能靠文字，模型很容易补上一只手或一个人。
        from app.services.generation import _spawn_asset_reference_tasks

        sheet_tasks = await _spawn_asset_reference_tasks(
            session,
            user_id=run.user_id,
            project_id=episode.project_id,
            assets=await asset_service.load_assets_without_reference(
                session, user_id=run.user_id, project_id=episode.project_id
            ),
            aspect_ratio=project.aspect_ratio if project else None,
        )
        if sheet_tasks:
            await _start_chain(session, sheet_tasks)
        return {"shot_count": len(shots), "plan": episode.plan}


async def _start_chain(session: AsyncSession, tasks: list) -> None:
    """把一批任务串成顺序链，只投递第一个。

    生图任务串行是为了让第 N 个镜头能引用第 N-1 个镜头刚落库的关键帧；
    视频同理，全部跑完后由导出任务收尾。
    """
    from app.services.dispatch import enqueue_task

    for current, nxt in zip(tasks, tasks[1:]):
        current.params = {**(current.params or {}), "chain_next": nxt.id}
    await session.commit()
    if tasks:
        await enqueue_task(session, tasks[0])


async def _batch_generate_images(state: PipelineState) -> dict[str, Any]:
    """第四步：确认后批量生图；只派发已确认（或显式指定）的镜头。"""
    from app.services.dispatch import create_task

    async with AsyncSessionLocal() as session:
        run = await session.get(PipelineRun, int(state["run_id"]))
        if not run:
            raise ProviderError("流水线记录不存在")
        params = run.params or {}
        episode = await session.get(Episode, int(state["episode_id"]))
        if not episode:
            raise ProviderError("分集不存在")

        shots = list(
            (
                await session.execute(
                    select(Shot).where(Shot.episode_id == episode.id).order_by(Shot.order_index)
                )
            ).scalars().all()
        )
        wanted = params.get("shot_ids")
        if wanted:
            wanted_set = {int(item) for item in wanted}
            picked = [shot for shot in shots if shot.id in wanted_set]
            targets = [shot for shot in picked if shot.prompt_confirmed]
            unconfirmed = [shot.id for shot in picked if not shot.prompt_confirmed]
            if not targets:
                raise ProviderError(
                    "所选镜头均未确认提示词，请先在分镜页确认后再批量生图"
                    + (f"（未确认镜头：{', '.join(str(i) for i in unconfirmed)}）" if unconfirmed else "")
                )
        else:
            targets = [shot for shot in shots if shot.prompt_confirmed]
            if not targets:
                raise ProviderError("还没有已确认提示词的镜头，请先在分镜页逐个确认")

        # 只带用户额外指定的资产；本项目的角色 / 道具由镜头的 subjects / props 自动解析，
        # 不再把全项目演员图都塞进去，避免模型把 A 角色的脸套到 B 角色身上。
        asset_ids = [int(item) for item in (params.get("asset_ids") or []) if item]
        asset_ids = sorted(set(asset_ids))

        tasks = []
        for shot in targets:
            tasks.append(
                await create_task(
                    session,
                    user_id=run.user_id,
                    kind="image",
                    provider=params.get("provider"),
                    model=params.get("model"),
                    project_id=episode.project_id,
                    episode_id=episode.id,
                    shot_id=shot.id,
                    enqueue=False,
                    params={
                        "shot_id": shot.id,
                        "credential_id": params.get("credential_id"),
                        "provider": params.get("provider"),
                        "model": params.get("model"),
                        "prompt": shot.image_prompt,
                        "n": 1,
                        "asset_ids": asset_ids or None,
                        "negative_prompt": params.get("negative_prompt"),
                        "flow": "pipeline_images",
                        "run_id": run.id,
                    },
                )
            )
            shot.status = "generating"

        run.status = "generating"
        run.stage = "generate"
        run.error = None
        await session.commit()
        await _start_chain(session, tasks)
        return {"generated": len(targets)}


async def _batch_generate_videos(state: PipelineState) -> dict[str, Any]:
    """第五步：为已有关键帧的镜头批量生视频，全部完成后自动拼成片。

    - 显式传 ``video_shot_ids``：只重跑这些镜头（即使已有视频）；
    - 不传：默认补齐该项目里「有关键帧但还没有视频」的所有镜头。
    """
    from app.services.dispatch import create_task

    async with AsyncSessionLocal() as session:
        run = await session.get(PipelineRun, int(state["run_id"]))
        if not run:
            raise ProviderError("流水线记录不存在")
        params = run.params or {}
        episode = await session.get(Episode, int(state["episode_id"]))
        if not episode:
            raise ProviderError("分集不存在")

        shots = list(
            (
                await session.execute(
                    select(Shot).where(Shot.episode_id == episode.id).order_by(Shot.order_index)
                )
            ).scalars().all()
        )
        if not shots:
            raise ProviderError("该分集还没有分镜，请先出分镜并生图")

        wanted = params.get("video_shot_ids")
        if wanted:
            wanted_set = {int(item) for item in wanted}
            targets = [shot for shot in shots if shot.id in wanted_set]
            if not targets:
                raise ProviderError("所选镜头不属于该分集")
        else:
            targets = [shot for shot in shots if shot.video_media_id is None]

        have_image = set(
            (
                await session.execute(
                    select(ShotKeyframe.shot_id)
                    .where(
                        ShotKeyframe.shot_id.in_([shot.id for shot in targets]),
                        ShotKeyframe.media_id.isnot(None),
                    )
                    .distinct()
                )
            ).scalars().all()
        ) if targets else set()
        targets = [shot for shot in targets if shot.id in have_image]
        if not targets:
            raise ProviderError("所选镜头都还没有关键帧，请先生成图片再批量生视频")

        asset_ids = sorted({int(item) for item in (params.get("asset_ids") or []) if item})
        tasks = []
        for shot in targets:
            tasks.append(
                await create_task(
                    session,
                    user_id=run.user_id,
                    kind="video",
                    provider=params.get("provider"),
                    model=params.get("model"),
                    project_id=episode.project_id,
                    episode_id=episode.id,
                    shot_id=shot.id,
                    enqueue=False,
                    params={
                        "shot_id": shot.id,
                        "credential_id": params.get("credential_id"),
                        "provider": params.get("provider"),
                        "model": params.get("model"),
                        "prompt": shot.video_prompt,
                        "duration": params.get("duration"),
                        "resolution": params.get("resolution") or shot.resolution,
                        "asset_ids": asset_ids or None,
                        "negative_prompt": params.get("negative_prompt"),
                        "flow": "pipeline_videos",
                        "run_id": run.id,
                    },
                )
            )
            shot.status = "generating"

        # 本项目所有镜头的视频都齐了才自动导出，避免拼出一部缺集的「半成品」
        target_ids = {shot.id for shot in targets}
        missing = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Shot)
                    .join(Episode, Shot.episode_id == Episode.id)
                    .where(
                        Episode.project_id == episode.project_id,
                        Shot.video_media_id.is_(None),
                        Shot.id.notin_(target_ids),
                    )
                )
            ).scalar_one()
        )
        export_task = None
        if missing == 0:
            export_task = await create_task(
                session,
                user_id=run.user_id,
                kind="export",
                project_id=episode.project_id,
                episode_id=episode.id,
                enqueue=False,
                params={"run_id": run.id, "flow": "pipeline_export"},
            )
            tasks.append(export_task)

        run.status = "generating"
        run.stage = "videos"
        run.error = None
        episode.status = "generating"
        await session.commit()
        await _start_chain(session, tasks)
        return {
            "generated": len(targets),
            "export_task_id": export_task.id if export_task else None,
        }


async def _json_call(
    provider: Any,
    prompt: str,
    *,
    system: str,
    model: str | None,
    temperature: float,
    max_tokens: int = 4096,
    attempts: int = 2,
) -> dict[str, Any]:
    """要求模型输出 JSON 并解析（本模块自带，避免与 generation 形成模块级互相依赖）。"""
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


# ---------------------------------------------------------------- 图
_GRAPH: Any = None


def _build_graph() -> Any:
    """构建并缓存 StateGraph；langgraph 缺失时抛出明确错误。"""
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于环境问题
        raise PipelineUnavailable(
            "制作流水线需要 langgraph 依赖，请执行 pip install -r requirements.txt 后重启服务"
        ) from exc

    graph = StateGraph(PipelineState)

    def _route(state: PipelineState) -> str:
        stage = state.get("stage")
        if stage == "confirmed":
            return "images"
        if stage == "videos":
            return "videos"
        return "characters"

    async def _entry(state: PipelineState) -> dict[str, Any]:
        return {}

    graph.add_node("route", _entry)
    graph.add_node("build_characters", _build_characters)
    graph.add_node("generate_breakdown", _generate_breakdown)
    graph.add_node("batch_generate_images", _batch_generate_images)
    graph.add_node("batch_generate_videos", _batch_generate_videos)

    graph.set_entry_point("route")
    graph.add_conditional_edges(
        "route",
        _route,
        {
            "characters": "build_characters",
            "images": "batch_generate_images",
            "videos": "batch_generate_videos",
        },
    )
    graph.add_edge("build_characters", "generate_breakdown")
    graph.add_edge("generate_breakdown", END)
    graph.add_edge("batch_generate_images", END)
    graph.add_edge("batch_generate_videos", END)

    _GRAPH = graph.compile()
    return _GRAPH


async def run_pipeline(run_id: int, stage: str) -> None:
    """按 stage 跑一遍流水线（start=建档案+出分镜；confirmed=批量生图；videos=批量生视频）。"""
    graph = _build_graph()
    async with AsyncSessionLocal() as session:
        run = await session.get(PipelineRun, run_id)
        if not run:
            return
        episode_id = run.episode_id
        if stage == "start":
            run.status, run.stage = "running", "characters"
        elif stage == "videos":
            run.status, run.stage = "generating", "videos"
        else:
            run.status, run.stage = "generating", "generate"
        run.error = None
        await session.commit()

    try:
        await graph.ainvoke(
            {"run_id": run_id, "episode_id": int(episode_id or 0), "stage": stage}
        )
    except Exception as exc:  # noqa: BLE001 - 任何阶段失败都要落库，供前端提示
        logger.exception("制作流水线执行失败 run=%s stage=%s", run_id, stage)
        async with AsyncSessionLocal() as session:
            run = await session.get(PipelineRun, run_id)
            if run:
                run.status = "failed"
                run.error = str(exc)[:1000]
                await session.commit()
        raise


async def plan_shot_images(session: AsyncSession, episode_id: int) -> list[Shot]:
    """辅助：取某分集里已确认提示词的镜头（供路由做参数校验 / 前端预览）。"""
    return list(
        (
            await session.execute(
                select(Shot)
                .where(Shot.episode_id == episode_id, Shot.prompt_confirmed.is_(True))
                .order_by(Shot.order_index)
            )
        ).scalars().all()
    )
