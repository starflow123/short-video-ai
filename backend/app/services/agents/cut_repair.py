"""切点修复 agent：读连续性审查 → 写视频提示词修正 →（可选）重拍下一镜 → 复审。

边界：
- 默认只给提示词修正，不重拍；重拍必须同时满足「用户请求开启」与「服务端开关开启」；
- 重拍会替换下一镜现有视频，属于真实费用与不可逆替换，因此在工具说明里显式标注；
- 重拍后必须调用复审工具，用同一套切点审查口径验证是否真的改善；
- 人的认可闸门不变：agent 不碰关键帧 approved_at，也不改导出结果。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.shot import Shot
from app.services.agents import runtime
from app.services.agents.audit import record_run
from app.services.agents import continuity
from app.services.providers.base import ProviderError

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是短剧剪辑的接戏修复顾问。你只根据切点审查给出的失败项，写一条针对下一镜"
    "视频提示词的增量修正；不评价画风，不改剧情、角色、人数或景别。"
    "每轮只输出一个 JSON：{\"thought\":\"...\",\"action\":\"工具名\",\"args\":{...}}。"
)

_POLICY = (
    "修复策略：\n"
    "1. 先 inspect 读取审查结论；只针对 fail / warn 的项写修正，pass 的项不要动。\n"
    "2. 修正是「下一镜视频提示词」的增量，例如「开场第一秒保持杨间穿黑色校服、柴刀在右手」。\n"
    "3. 允许重拍时，写完 patch 再调用 rerender_video；重拍后必须 recheck_cut 复审。\n"
    "4. 不允许重拍时，写完 patch 直接 report，把修正交给用户手动重跑。\n"
    "5. 姿态变化、景别切换不是问题，不要试图修正它们。\n"
    "6. 复审没有改善就如实 report / escalate，不要反复重拍（最多 1 次）。"
)

_RANK = {"pass": 0, "warn": 1, "fail": 2, "unknown": 1}


@dataclass
class _CutContext:
    session: AsyncSession
    user_id: int
    task_id: int
    shot_id: int
    prev_shot_id: int | None
    allow_rerender: bool
    video_credential_id: int | None
    video_provider: str | None
    video_model: str | None
    before: dict[str, Any]
    patch: str = ""
    rerendered: bool = False
    new_media_id: int | None = None
    rerender_note: str = ""
    after: dict[str, Any] | None = None
    summary: str = ""
    notes: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _failed_checks(review: dict[str, Any]) -> list[str]:
    checks = review.get("checks") if isinstance(review.get("checks"), dict) else {}
    labels = []
    for key, value in checks.items():
        if str(value) in ("fail", "warn"):
            labels.append(f"{continuity.CHECK_LABELS.get(str(key), str(key))}：{value}")
    return labels


def _review_text(review: dict[str, Any] | None) -> str:
    if not isinstance(review, dict):
        return "（无审查结论）"
    lines = [f"总结论：{_text(review.get('verdict')) or 'unknown'}"]
    checks = review.get("checks") if isinstance(review.get("checks"), dict) else {}
    for key, value in checks.items():
        lines.append(f"- {continuity.CHECK_LABELS.get(str(key), str(key))}：{value}")
    reasons = [_text(item) for item in _as_list(review.get("reasons")) if _text(item)]
    if reasons:
        lines.append("原因：" + "；".join(reasons))
    return "\n".join(lines)


def _verdict_rank(review: dict[str, Any] | None) -> int:
    verdict = _text((review or {}).get("verdict"))
    return _RANK.get(verdict, 1)


async def _load_cut_review(
    session: AsyncSession,
    *,
    user_id: int,
    shot: Shot,
    force: bool = False,
) -> tuple[Shot, dict[str, Any]]:
    """确保拿到本镜与上一镜之间最新的切点审查；缺失时现场跑一次。"""
    prev = await continuity.previous_shot(session, shot)
    if prev is None:
        raise ProviderError("这是第一个镜头，没有可修复的切点")
    asset = await continuity._video_asset(session, shot)
    prev_asset = await continuity._video_asset(session, prev)
    if asset is None or prev_asset is None:
        raise ProviderError("相邻镜头缺少可用视频文件，无法审查切点")
    existing = (asset.meta or {}).get("continuity_review")
    if not force and isinstance(existing, dict) and continuity._fresh(asset, prev_asset):
        return prev, existing
    report = await continuity.review_cut(
        session, user_id=user_id, prev_shot=prev, shot=shot, force=True
    )
    if not isinstance(report, dict):
        raise ProviderError("切点审查没有返回结论")
    return prev, report


def _report(args: dict[str, Any]) -> str:
    return _text(args.get("summary"))[:600]


async def run_cut_repair(
    session: AsyncSession,
    *,
    user_id: int,
    shot_id: int,
    task_id: int,
    allow_rerender: bool = False,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
    video_credential_id: int | None = None,
    video_provider: str | None = None,
    video_model: str | None = None,
) -> dict[str, Any]:
    """执行一次切点修复；默认只产出提示词修正。"""
    from app.services.generation import _generate_json, build_provider_from, resolve_credential

    shot = await session.get(Shot, int(shot_id))
    if shot is None:
        raise ProviderError("分镜不存在")
    project_id: int | None = None
    if shot.episode_id:
        from app.models.episode import Episode

        episode = await session.get(Episode, shot.episode_id)
        project_id = episode.project_id if episode else None
    try:
        prev, review = await _load_cut_review(session, user_id=user_id, shot=shot)
    except (ProviderError, ValueError) as exc:
        logger.warning("分镜 %s 切点修复无法取得审查：%s", shot_id, exc)
        await record_run(
            session,
            user_id=user_id,
            kind="cut_repair",
            status="review_unavailable",
            project_id=project_id,
            target_type="shot",
            target_id=int(shot_id),
            summary={"message": str(exc)[:300]},
            trace=[],
        )
        return {
            "action": "cut_repair",
            "status": "review_unavailable",
            "shot_id": int(shot_id),
            "steps": 0,
            "trace": [],
            "advisory": True,
            "message": f"无法取得切点审查，已转人工：{exc}",
        }

    before = review
    if _text(before.get("verdict")) == "pass":
        message = "这个切点审查已通过，不需要修复。"
        await record_run(
            session,
            user_id=user_id,
            kind="cut_repair",
            status="already_pass",
            project_id=project_id,
            target_type="shot",
            target_id=int(shot_id),
            summary={"review": before, "message": message},
            trace=[],
        )
        return {
            "action": "cut_repair",
            "status": "already_pass",
            "shot_id": int(shot_id),
            "review": before,
            "steps": 0,
            "trace": [],
            "advisory": True,
            "message": message,
        }

    ctx = _CutContext(
        session=session,
        user_id=user_id,
        task_id=task_id,
        shot_id=int(shot_id),
        prev_shot_id=int(prev.id),
        allow_rerender=bool(allow_rerender),
        video_credential_id=video_credential_id,
        video_provider=video_provider,
        video_model=video_model,
        before=before,
    )

    try:
        credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    except ProviderError as exc:
        logger.warning("分镜 %s 切点修复缺少文本凭证：%s", shot_id, exc)
        result = {
            "action": "cut_repair",
            "status": "review_unavailable",
            "shot_id": int(shot_id),
            "review": before,
            "steps": 0,
            "trace": [],
            "advisory": True,
            "message": f"无法使用文本模型写修正：{exc}",
        }
        await record_run(
            session,
            user_id=user_id,
            kind="cut_repair",
            status="review_unavailable",
            target_type="shot",
            target_id=int(shot_id),
            summary={"review": before, "message": str(exc)[:300]},
            trace=[],
        )
        return result

    provider = build_provider_from(credential)
    decision_model = model or provider.model_for("text")

    async def decide(prompt: str) -> dict[str, Any]:
        return await _generate_json(
            provider,
            prompt,
            system=_SYSTEM,
            model=decision_model,
            temperature=0.2,
            max_tokens=1200,
            attempts=2,
        )

    async def inspect(_: dict[str, Any]) -> runtime.ToolResult:
        current_shot = await session.get(Shot, ctx.shot_id)
        current_prev = await session.get(Shot, ctx.prev_shot_id) if ctx.prev_shot_id else None
        lines = ["切点审查结论：", _review_text(ctx.before)]
        if current_prev is not None:
            lines.extend(continuity._shot_block(current_prev, label="上一镜"))
        if current_shot is not None:
            lines.extend(continuity._shot_block(current_shot, label="下一镜"))
            lines.append(
                "下一镜视频提示词："
                + (_text(current_shot.video_prompt) or "（空，按分镜描述生成）")[:600]
            )
            keyframes = (await load_approved_keyframe_labels(session, current_shot)).strip()
            if keyframes:
                lines.append(f"下一镜已认可关键帧：{keyframes}")
        lines.append(f"允许重拍：{'是' if ctx.allow_rerender else '否'}")
        return runtime.ToolResult("\n".join(lines))

    async def patch_prompt(args: dict[str, Any]) -> runtime.ToolResult:
        patch = _text(args.get("patch"))[:240]
        if not patch:
            return runtime.ToolResult("patch 不能为空。", ok=False)
        if patch == ctx.patch:
            return runtime.ToolResult("和上一次 patch 完全相同，请针对失败项改写。", ok=False)
        ctx.patch = patch
        return runtime.ToolResult(f"已记录修正：{patch}。")

    async def rerender_video(_: dict[str, Any]) -> runtime.ToolResult:
        if not ctx.allow_rerender:
            return runtime.ToolResult("本次未开启重拍，只给修正建议；用户可手动重跑下一镜。", ok=False)
        if not settings.cut_repair_rerender_enabled:
            return runtime.ToolResult(
                "服务端未开启「切点修复允许重拍」（CUT_REPAIR_RERENDER_ENABLED），只给建议。",
                ok=False,
            )
        if not ctx.patch:
            return runtime.ToolResult("还没有 patch，先写 patch_prompt。", ok=False)
        if ctx.rerendered:
            return runtime.ToolResult("本次已经重拍过一次，不能重复重拍。", ok=False)

        from app.services.generation import _handle_video, _project_of_shot

        current_shot = await session.get(Shot, ctx.shot_id)
        if current_shot is None:
            return runtime.ToolResult("分镜已不存在。", ok=False)
        project = await _project_of_shot(session, current_shot)
        child = SimpleNamespace(
            id=ctx.task_id,
            user_id=ctx.user_id,
            project_id=project.id if project else None,
            provider=ctx.video_provider,
            model=ctx.video_model,
            params={
                "shot_id": ctx.shot_id,
                "repair_patch": ctx.patch,
                "credential_id": ctx.video_credential_id,
                "provider": ctx.video_provider,
                "model": ctx.video_model,
            },
        )
        result = await _handle_video(session, child)
        ctx.rerendered = True
        ctx.new_media_id = int(result["media_id"])
        ctx.rerender_note = "重拍已替换下一镜现有视频"
        return runtime.ToolResult(
            f"已重拍下一镜：media={ctx.new_media_id}；旧视频若无人引用已清理。请调用 recheck_cut 复审。",
            data={"media_id": ctx.new_media_id},
        )

    async def recheck_cut(_: dict[str, Any]) -> runtime.ToolResult:
        if not ctx.rerendered:
            return runtime.ToolResult("还没有重拍，当前审查结论仍有效。", ok=False)
        current_shot = await session.get(Shot, ctx.shot_id)
        if current_shot is None:
            return runtime.ToolResult("分镜已不存在。", ok=False)
        try:
            _, report = await _load_cut_review(
                session, user_id=ctx.user_id, shot=current_shot, force=True
            )
        except (ProviderError, ValueError) as exc:
            return runtime.ToolResult(f"复审失败：{exc}", ok=False)
        ctx.after = report
        return runtime.ToolResult("复审结论：\n" + _review_text(report), data={"review": report})

    async def report_tool(args: dict[str, Any]) -> runtime.ToolResult:
        ctx.summary = _report(args) or "已给出提示词修正，建议按修正重跑下一镜。"
        if ctx.after is not None:
            after_rank = _verdict_rank(ctx.after)
            before_rank = _verdict_rank(ctx.before)
            if after_rank < before_rank:
                status = "repaired" if _text(ctx.after.get("verdict")) == "pass" else "improved"
            else:
                status = "needs_human"
            if status == "needs_human":
                ctx.notes.append("重拍后复审没有改善，建议人工检查场景 / 服装设定")
        else:
            status = "patch_only"
        return runtime.ToolResult(
            f"已记录修复结论（{status}）。",
            data={
                "status": status,
                "patch": ctx.patch,
                "before": ctx.before,
                "after": ctx.after,
                "rerendered": ctx.rerendered,
                "new_media_id": ctx.new_media_id,
                "notes": ctx.notes,
                "summary": ctx.summary,
            },
            done=True,
            status=status,
        )

    async def escalate(args: dict[str, Any]) -> runtime.ToolResult:
        reason = _text(args.get("reason"))[:500] or "需要人工判断"
        ctx.summary = reason
        return runtime.ToolResult(
            f"已转人工：{reason}",
            data={
                "status": "needs_human",
                "patch": ctx.patch,
                "before": ctx.before,
                "after": ctx.after,
                "rerendered": ctx.rerendered,
                "new_media_id": ctx.new_media_id,
                "notes": ctx.notes,
                "summary": reason,
            },
            done=True,
            status="needs_human",
        )

    tools = [
        runtime.AgentTool(
            "inspect", "读取切点审查结论、前后镜头约束与下一镜视频提示词", {}, inspect, kind="read", max_calls=2
        ),
        runtime.AgentTool(
            "patch_prompt",
            "写一条针对失败项的视频提示词增量修正（最多 240 字）",
            {"patch": "具体修正"},
            patch_prompt,
            kind="write",
            max_calls=2,
        ),
        runtime.AgentTool(
            "rerender_video",
            "按当前 patch 重拍下一镜视频（会替换现有视频并产生费用，需显式开启）",
            {},
            rerender_video,
            kind="expensive",
            max_calls=1,
        ),
        runtime.AgentTool(
            "recheck_cut",
            "重拍后重新比对尾帧/首帧，验证是否真的改善",
            {},
            recheck_cut,
            kind="expensive",
            max_calls=1,
        ),
        runtime.AgentTool(
            "report",
            "结束并记录结论；参数 summary 写清修正内容与用户下一步",
            {"summary": "结论与建议"},
            report_tool,
            kind="write",
            max_calls=1,
        ),
        runtime.AgentTool(
            "escalate",
            "放弃自动修复，交给人工（写明原因）",
            {"reason": "为什么需要人工"},
            escalate,
            kind="read",
            max_calls=1,
        ),
    ]

    failed = _failed_checks(ctx.before)
    run = await runtime.run_agent(
        goal=(
            "修复这个切点的人物 / 服装 / 场景 / 光线断裂：先写视频提示词修正；"
            "允许重拍时最多重拍一次并复审，否则只给建议。"
        ),
        tools=tools,
        decide=decide,
        initial_observation=(
            f"切点审查（失败项：{('；'.join(failed) or '无明确失败项')}）\n"
            f"{_review_text(ctx.before)}\n"
            f"允许重拍：{'是' if ctx.allow_rerender else '否'}\n\n{_POLICY}"
        ),
        max_steps=6,
        max_expensive_calls=2,
    )

    final_data = run.final if isinstance(run.final, dict) else {}
    if run.status == "agent_error":
        status = "agent_error"
    elif ctx.after is not None:
        after_rank = _verdict_rank(ctx.after)
        if after_rank < _verdict_rank(ctx.before):
            status = "repaired" if _text(ctx.after.get("verdict")) == "pass" else "improved"
        else:
            status = "needs_human"
    elif ctx.rerendered:
        status = "needs_human"
    elif run.status in ("needs_human", "escalated"):
        status = "needs_human"
    elif ctx.patch:
        status = "patch_only"
    else:
        status = "needs_human"

    result = {
        "action": "cut_repair",
        "status": status,
        "shot_id": ctx.shot_id,
        "from_shot_id": ctx.prev_shot_id,
        "patch": ctx.patch,
        "before": ctx.before,
        "after": ctx.after,
        "rerendered": ctx.rerendered,
        "new_media_id": ctx.new_media_id,
        "notes": ctx.notes,
        "summary": ctx.summary or _text(final_data.get("summary")),
        "allow_rerender": ctx.allow_rerender,
        "model": decision_model,
        "steps": run.steps,
        "stop_reason": run.stop_reason,
        "trace": run.trace,
        "advisory": True,
    }
    if run.status == "agent_error":
        result["error"] = (run.final or {}).get("error")
    logger.info(
        "切点修复完成：shot=%s status=%s rerendered=%s steps=%s",
        ctx.shot_id,
        status,
        ctx.rerendered,
        run.steps,
    )
    await record_run(
        session,
        user_id=user_id,
        kind="cut_repair",
        status=status,
        project_id=project_id,
        target_type="shot",
        target_id=ctx.shot_id,
        model=decision_model,
        steps=run.steps,
        stop_reason=run.stop_reason,
        summary={
            "patch": ctx.patch,
            "before": ctx.before,
            "after": ctx.after,
            "rerendered": ctx.rerendered,
            "new_media_id": ctx.new_media_id,
            "allow_rerender": ctx.allow_rerender,
            "summary": result["summary"],
        },
        trace=run.trace,
        error=result.get("error"),
    )
    return result


async def load_approved_keyframe_labels(session: AsyncSession, shot: Shot) -> str:
    from app.services.keyframes import load_keyframes

    keyframes = (await load_keyframes(session, [shot.id])).get(shot.id, [])
    labels = [
        _text(item.label) or f"media {item.media_id}"
        for item in keyframes
        if item.approved_at and item.media_id
    ]
    return "；".join(labels)
