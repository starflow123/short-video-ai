"""关键帧修复 agent：读审查报告 → 改提示词 → 重绘 → 复审 → 再决策。

和 keyframe / continuity 两个「单次审查器」的区别：
- 有工具选择：inspect / patch_prompt / redraw / review_candidate / escalate；
- 有闭环：重绘后必须复审，失败可以再改一轮；
- 有预算：最多重绘 2 次、最多 6 步，模型不能无限烧钱；
- 有人工闸门：只产出「候选帧」，永远不写 approved_at，也不替换旧帧。

候选人需要用户对比后自己点「认可」；批量生视频仍然只消费已认可的帧。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.episode import Episode
from app.models.keyframe import ShotKeyframe
from app.models.media import MediaAsset
from app.models.project import Project
from app.models.shot import Shot
from app.services.agents import runtime
from app.services.agents.audit import record_run
from app.services.agents.visual_qa import CHECK_LABELS, review_keyframe
from app.services.providers.base import ProviderError

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是短剧关键帧修复 agent。你只做一件事：根据视觉初审的失败项，修改画面提示词，"
    "再调用工具重绘并复审。你不能改剧情、不能增删角色或道具、不能改人数，"
    "也不能声称已经认可画面——认可只能由人做。"
    "每轮只输出一个 JSON：{\"thought\":\"...\",\"action\":\"工具名\",\"args\":{...}}。"
)

_POLICY = (
    "修复策略：\n"
    "1. 先根据失败项写一条具体的增量修正（patch_prompt），只修正失败项，不要重写整段提示词。\n"
    "2. 然后调用 redraw 生成候选帧；候选帧会追加在关键帧列表里，不替换旧帧。\n"
    "3. 重绘后必须调用 review_candidate 复审；复审通过再 finish，未通过可以再改一轮。\n"
    "4. 最多重绘 2 次。同一个问题连续两次没修好、或缺少图片/视觉凭证时，调用 escalate。\n"
    "5. 人物脸型、服装、场景是硬约束；修正描述里不要出现「换一个人」「改成另一个场景」。\n"
    "6. 不要重复提交完全相同的 patch；第二次修正必须针对新的失败原因。"
)


async def _lookup_lessons(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int | None,
    media_type: str,
    visual_style: str | None = None,
) -> list:
    from app.services import knowledge as wiki
    from app.services.knowledge import lookup

    try:
        notes = await lookup(
            session,
            user_id=user_id,
            project_id=project_id,
            visual_style=visual_style,
            context={"media_type": media_type},
            category="heuristic",
        )
        await wiki.mark_used(session, [note.id for note in notes])
        return notes
    except Exception as exc:  # noqa: BLE001 - 教训库是增强能力
        logger.warning("修复 agent 知识检索跳过：%s", exc)
        await session.rollback()
        return []


def _lessons_block(lessons: list) -> str:
    lines = [f"- {note.title}：{note.content}" for note in lessons[:5]]
    return "【生产教训（已认可的历史结论，修复时参考）】\n" + "\n".join(lines)


@dataclass
class _RepairContext:
    session: AsyncSession
    user_id: int
    shot: Shot
    keyframe: ShotKeyframe
    task_id: int
    max_rounds: int
    patch: str = ""
    candidate_media_id: int | None = None
    candidate_keyframe_id: int | None = None
    candidate_url: str | None = None
    review: dict[str, Any] | None = None
    redraws: int = 0
    last_redraw: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _subject_lines(shot: Shot) -> list[str]:
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


def _prop_lines(shot: Shot) -> list[str]:
    lines: list[str] = []
    for prop in _as_list(shot.props)[:6]:
        if not isinstance(prop, dict):
            continue
        name = _text(prop.get("name"))
        if not name:
            continue
        owner = _text(prop.get("owner"))
        state = _text(prop.get("state"))
        line = name
        if owner:
            line += f"（应由 {owner} 持有）"
        if state:
            line += f"，状态：{state}"
        lines.append(line)
    return lines


def _review_lines(review: dict[str, Any] | None) -> list[str]:
    if not isinstance(review, dict):
        return ["尚无审查结论"]
    checks = review.get("checks") if isinstance(review.get("checks"), dict) else {}
    lines = [f"总结论：{_text(review.get('verdict')) or 'unknown'}"]
    for key, value in checks.items():
        label = CHECK_LABELS.get(str(key), str(key))
        if str(value) in ("fail", "warn", "unknown"):
            lines.append(f"- {label}：{value}")
    reasons = [_text(item) for item in _as_list(review.get("reasons")) if _text(item)]
    if reasons:
        lines.append("原因：" + "；".join(reasons))
    patch = _text(review.get("prompt_patch"))
    if patch:
        lines.append(f"初审建议的修正：{patch}")
    return lines


def build_initial_observation(
    *,
    shot: Shot,
    keyframe: ShotKeyframe,
    review: dict[str, Any] | None,
    max_rounds: int,
) -> str:
    """把分镜硬约束 + 当前审查失败项交给决策模型；这是 agent 的「观察」。"""
    lines = [
        f"镜头：{_text(shot.title) or '未命名'}",
        f"画面描述：{_text(shot.image_prompt or shot.description)}",
    ]
    subjects = _subject_lines(shot)
    if subjects:
        lines.append("必须保持不变的角色：" + "；".join(subjects))
    props = _prop_lines(shot)
    if props:
        lines.append("道具与归属：" + "；".join(props))
    directives = shot.directives if isinstance(shot.directives, dict) else {}
    constraints = []
    for key, label in (("shot_size", "景别"), ("camera_angle", "机位"), ("composition", "构图")):
        value = _text(directives.get(key))
        if value:
            constraints.append(f"{label}：{value}")
    if constraints:
        lines.append("构图约束：" + "；".join(constraints))
    lines.append(f"当前关键帧：{_text(keyframe.label) or keyframe.id}（media {keyframe.media_id}）")
    lines.append("当前提示词：" + (_text(keyframe.prompt) or "（空，按分镜描述生成）"))
    lines.extend(_review_lines(review))
    lines.append(f"预算：最多重绘 {max_rounds} 次；每次重绘后必须复审。")
    return "\n".join(lines)


async def _load_review(
    session: AsyncSession,
    *,
    user_id: int,
    shot: Shot,
    keyframe: ShotKeyframe,
) -> tuple[MediaAsset, dict[str, Any]]:
    if not keyframe.media_id:
        raise ProviderError("这一帧还没有图片，无法修复")
    asset = await session.get(MediaAsset, int(keyframe.media_id))
    if not asset:
        raise ProviderError("关键帧素材不存在")
    review = (asset.meta or {}).get("visual_review")
    if not isinstance(review, dict):
        review = await review_keyframe(session, user_id=user_id, asset=asset, shot=shot)
    return asset, review


async def _redraw_candidate(ctx: _RepairContext) -> runtime.ToolResult:
    """真正重绘一张候选帧：追加新关键帧，保留旧帧，不自动认可。"""
    # 延迟导入：generation 在生图链路里会调用 agents，模块级 import 会成环
    from app.services.generation import _handle_image, _project_of_shot

    if not ctx.patch:
        return runtime.ToolResult(
            "还没有 patch。先调用 patch_prompt 写一条具体修正，再重绘。",
            ok=False,
        )
    if ctx.redraws >= ctx.max_rounds:
        return runtime.ToolResult(
            f"重绘次数已达上限（{ctx.max_rounds} 次），请复审或 escalate。",
            ok=False,
        )

    base = (ctx.keyframe.prompt or ctx.shot.image_prompt or ctx.shot.description or "").strip()
    if not base:
        return runtime.ToolResult("缺少基础提示词，无法重绘。", ok=False)
    project = await _project_of_shot(ctx.session, ctx.shot)

    child = SimpleNamespace(
        id=ctx.task_id,
        user_id=ctx.user_id,
        project_id=project.id if project else None,
        provider=None,
        model=None,
        params={
            "shot_id": ctx.shot.id,
            "prompt": base,
            # 修正说明单独走 extra 层：正文超 300 字会被预算按句裁剪，
            # 拼在正文尾巴上可能整段被切掉，agent 改了等于没改。
            "repair_patch": ctx.patch,
            "append": True,
            # 修复候选不能承接「有缺陷的上一张」，也不要替换用户原来的帧
            "repair_candidate": True,
            # 复审由 agent 显式调用，避免生图链路自动再审一次造成重复扣费
            "skip_auto_review": True,
            "keyframe_label": f"AI 修复候选 {ctx.redraws + 1}",
        },
    )
    result = await _handle_image(ctx.session, child)
    ctx.redraws += 1
    ctx.candidate_media_id = int(result["media_id"])
    ctx.candidate_keyframe_id = result.get("keyframe_id")
    ctx.candidate_url = result.get("url")
    ctx.last_redraw = {
        "media_id": ctx.candidate_media_id,
        "keyframe_id": ctx.candidate_keyframe_id,
        "reference_used": result.get("reference_used"),
        "reference_count": result.get("reference_count"),
        "reference_warning": result.get("warning"),
        "reference_issues": result.get("reference_issues") or [],
    }
    warn = _text(result.get("warning") or "")
    return runtime.ToolResult(
        "候选帧已生成并追加到关键帧列表："
        f"keyframe={ctx.candidate_keyframe_id} media={ctx.candidate_media_id}；"
        + (f"注意：{warn}" if warn else "请调用 review_candidate 复审。"),
        data=ctx.last_redraw,
    )


async def _review_candidate(ctx: _RepairContext) -> runtime.ToolResult:
    if not ctx.candidate_media_id:
        return runtime.ToolResult("还没有候选帧，先调用 redraw。", ok=False)
    asset = await ctx.session.get(MediaAsset, int(ctx.candidate_media_id))
    if not asset:
        return runtime.ToolResult("候选帧素材不存在，请重新 redraw。", ok=False)
    review = await review_keyframe(
        ctx.session,
        user_id=ctx.user_id,
        asset=asset,
        shot=ctx.shot,
    )
    ctx.review = review
    failures = [
        CHECK_LABELS.get(str(key), str(key))
        for key, value in (review.get("checks") or {}).items()
        if str(value) == "fail"
    ]
    warns = [
        CHECK_LABELS.get(str(key), str(key))
        for key, value in (review.get("checks") or {}).items()
        if str(value) == "warn"
    ]
    detail = "；".join(f"- {label}" for label in failures + warns) or "无失败项"
    return runtime.ToolResult(
        f"复审结论：{review.get('verdict')}； {detail}；"
        f"原因：{'；'.join(_text(item) for item in _as_list(review.get('reasons'))) or '无'}",
        data={"review": review},
    )


def _final_status(run_status: str, ctx: _RepairContext) -> str:
    if ctx.review and _text(ctx.review.get("verdict")) == "pass":
        return "repaired"
    if ctx.candidate_media_id:
        return "needs_human"
    if run_status in ("escalated", "step_limit", "agent_error"):
        return run_status
    return "needs_human" if ctx.candidate_media_id else "escalated"


async def run_repair(
    session: AsyncSession,
    *,
    user_id: int,
    shot: Shot,
    keyframe: ShotKeyframe,
    task_id: int,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
    max_rounds: int | None = None,
) -> dict[str, Any]:
    """执行一次修复 agent；返回 trace 与候选帧，不写 approved_at。"""
    from app.services.generation import _generate_json, build_provider_from, resolve_credential

    rounds = max(1, min(int(max_rounds or settings.agent_repair_max_rounds), 3))
    project_id = None
    style_context: str | None = None
    if shot.episode_id:
        episode = await session.get(Episode, shot.episode_id)
        project_id = episode.project_id if episode else None
    if project_id:
        project = await session.get(Project, project_id)
        if project:
            # visual_style 是长描述、style 是短标签，两个都参与画风桶匹配
            style_context = " ".join(
                item for item in (project.visual_style, project.style) if item
            ) or None
    try:
        asset, review = await _load_review(
            session, user_id=user_id, shot=shot, keyframe=keyframe
        )
    except (ProviderError, ValueError) as exc:
        # 没有视觉凭证 / 图片读不出来时不做假装修复：如实转人工，任务本身不算失败
        logger.warning("修复 agent 无法取得初审：shot=%s keyframe=%s err=%s", shot.id, keyframe.id, exc)
        await record_run(
            session,
            user_id=user_id,
            kind="repair",
            status="review_unavailable",
            project_id=project_id,
            target_type="keyframe",
            target_id=keyframe.id,
            summary={"shot_id": shot.id, "message": str(exc)[:300]},
            trace=[],
        )
        return {
            "action": "repair",
            "status": "review_unavailable",
            "steps": 0,
            "trace": [],
            "advisory": True,
            "message": f"无法取得视觉初审，已转人工：{exc}",
        }
    if _text(review.get("verdict")) == "pass":
        await record_run(
            session,
            user_id=user_id,
            kind="repair",
            status="already_pass",
            project_id=project_id,
            target_type="keyframe",
            target_id=keyframe.id,
            summary={"shot_id": shot.id, "review": review},
            trace=[],
        )
        return {
            "action": "repair",
            "status": "already_pass",
            "steps": 0,
            "trace": [],
            "review": review,
            "advisory": True,
            "message": "初审已通过，不需要修复；认可仍由你决定。",
        }

    credential = await resolve_credential(
        session, user_id, "text", credential_id, provider_key
    )
    provider = build_provider_from(credential)
    decision_model = model or provider.model_for("text")

    # 资格门检索生产教训：只注入已认可 + active + 前置条件成立的条目，凑不齐就一个都没有
    lessons = await _lookup_lessons(
        session,
        user_id=user_id,
        project_id=project_id,
        media_type="image",
        visual_style=style_context,
    )

    async def decide(prompt: str) -> dict[str, Any]:
        system = _SYSTEM if not lessons else f"{_SYSTEM}\n\n{_lessons_block(lessons)}"
        return await _generate_json(
            provider,
            prompt,
            system=system,
            model=decision_model,
            temperature=0.2,
            max_tokens=1200,
            attempts=2,
        )

    ctx = _RepairContext(
        session=session,
        user_id=user_id,
        shot=shot,
        keyframe=keyframe,
        task_id=task_id,
        max_rounds=rounds,
        review=review,
    )

    async def inspect(_: dict[str, Any]) -> runtime.ToolResult:
        current = ctx.review or review
        return runtime.ToolResult(
            build_initial_observation(
                shot=shot, keyframe=keyframe, review=current, max_rounds=rounds
            )
        )

    async def patch_prompt(args: dict[str, Any]) -> runtime.ToolResult:
        patch = _text(args.get("patch"))[:200]
        if not patch:
            return runtime.ToolResult("patch 不能为空。", ok=False)
        if patch == ctx.patch:
            return runtime.ToolResult("和上一次 patch 完全相同，请针对新的失败原因改写。", ok=False)
        ctx.patch = patch
        return runtime.ToolResult(f"已记录修正描述：{patch}。下一步调用 redraw。")

    tools = [
        runtime.AgentTool(
            "inspect",
            "重新读取镜头约束与当前审查结论",
            {},
            inspect,
            kind="read",
            max_calls=2,
        ),
        runtime.AgentTool(
            "patch_prompt",
            "写一条只针对失败项的增量修正，不重写整段提示词",
            {"patch": "具体修正，最多 200 字"},
            patch_prompt,
            kind="write",
            max_calls=rounds + 1,
        ),
        runtime.AgentTool(
            "redraw",
            "用当前 patch 重绘一张候选关键帧（会真实产生图片费用）",
            {},
            lambda _: _redraw_candidate(ctx),
            kind="expensive",
            max_calls=rounds,
        ),
        runtime.AgentTool(
            "review_candidate",
            "对候选帧做视觉初审，返回通过/失败项与原因",
            {},
            lambda _: _review_candidate(ctx),
            kind="read",
            max_calls=rounds + 1,
        ),
        runtime.AgentTool(
            "escalate",
            "放弃自动修复，交给人工（写明原因）",
            {"reason": "为什么需要人工"},
            lambda args: _escalate(args),
            kind="read",
            max_calls=1,
        ),
    ]

    observation = build_initial_observation(
        shot=shot, keyframe=keyframe, review=review, max_rounds=rounds
    )
    run = await runtime.run_agent(
        goal=(
            "修复这张关键帧的失败项：先写 patch_prompt，再 redraw，然后 review_candidate；"
            "最多重绘 2 次，修不好就 escalate。不要改剧情、人数、角色或道具归属。"
        ),
        tools=tools,
        decide=decide,
        initial_observation=f"{observation}\n\n{_POLICY}",
        max_steps=8,
        max_expensive_calls=rounds,
    )

    final = {
        "action": "repair",
        "status": _final_status(run.status, ctx),
        "steps": run.steps,
        "stop_reason": run.stop_reason,
        "trace": run.trace,
        "initial_review": review,
        "review": ctx.review,
        "prompt_patch": ctx.patch,
        "redraws": ctx.redraws,
        "candidate_keyframe_id": ctx.candidate_keyframe_id,
        "candidate_media_id": ctx.candidate_media_id,
        "candidate_url": ctx.candidate_url,
        "advisory": True,
    }
    if ctx.last_redraw:
        final["last_redraw"] = ctx.last_redraw
    if run.status == "agent_error":
        final["error"] = (run.final or {}).get("error")
    await record_run(
        session,
        user_id=user_id,
        kind="repair",
        status=final["status"],
        project_id=project_id,
        target_type="keyframe",
        target_id=keyframe.id,
        model=decision_model,
        steps=run.steps,
        stop_reason=run.stop_reason,
        summary={
            "shot_id": shot.id,
            "prompt_patch": ctx.patch,
            "initial_verdict": (review or {}).get("verdict"),
            "final_verdict": (ctx.review or {}).get("verdict"),
            "redraws": ctx.redraws,
            "candidate_keyframe_id": ctx.candidate_keyframe_id,
            "candidate_media_id": ctx.candidate_media_id,
        },
        trace=run.trace,
        error=final.get("error"),
    )
    logger.info(
        "修复 agent 完成：shot=%s keyframe=%s status=%s steps=%s redraws=%s",
        shot.id,
        keyframe.id,
        final["status"],
        run.steps,
        ctx.redraws,
    )
    return final


async def _escalate(args: dict[str, Any]) -> runtime.ToolResult:
    reason = _text(args.get("reason"))[:500] or "模型判断需要人工处理"
    return runtime.ToolResult(
        f"已转人工：{reason}",
        data={"reason": reason},
        done=True,
        status="escalated",
    )
