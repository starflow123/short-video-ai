"""导演自检 agent：拆解方案先过确定性校验，有硬伤才让模型修订（最多 2 轮）。

与「一次生成方案」的区别：
- inspect / validate 是确定性工具，镜头数、总时长、段落结构由代码算，不靠模型感觉；
- revise 才调用模型，且只看校验发现和剧本，按最小改动修方案；
- 修完必须重新 validate；仍有硬伤就如实标 needs_human，不假装通过；
- 最终只返回「评审意见 + 修订稿」，不动库，仍由用户确认后才拆解。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.episode import Episode
from app.services import prompts
from app.services.agents import runtime
from app.services.agents.audit import record_run

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是短剧分镜统筹，负责修订拆解方案。你只改「拆几镜、每段几镜、每镜多长、"
    "段落顺序与摘要」，不写分镜正文。每轮只输出一个 JSON："
    '{"thought":"...","action":"工具名","args":{...}}。'
)

_POLICY = (
    "自检策略：\n"
    "1. 先用 validate 看确定性校验结果；没有硬伤就 finish(status=clean)。\n"
    "2. 有硬伤时调用 revise 修方案：镜头数之和必须等于 shot_count，"
    "段落要覆盖剧本的关键情节转折，单镜时长保持 5-12 秒。\n"
    "3. 每次 revise 后必须再 validate；还剩硬伤就再修一轮，最多 2 轮。\n"
    "4. 不要为了过校验删掉关键情节；宁可拆细，也不要合并掉冲突与反转。\n"
    "5. 修不好或剧本信息不足时 finish(status=needs_human)，并说明原因。"
)

_REVISE_PROMPT = """请修订下面这份分镜拆解方案。

【硬约束】
- 镜头总数必须为 {shot_count}，各段 shots 之和必须正好等于它。
- 单镜建议时长 {shot_seconds} 秒（5-12 秒之间），成片目标时长约 {target_seconds} 秒。
- 段落数 3-6 段，按剧本情节顺序，覆盖开场钩子、冲突升级、转折、收束。
- 只输出规划，不写分镜正文、不写提示词。

【当前方案】
{plan_json}

【校验发现的问题】
{issues}

【剧本】
{script}

只输出 JSON（不要 Markdown）：
{{"reasoning":"为什么这样改","shot_count":{shot_count},"shot_seconds":{shot_seconds},"target_seconds":{target_seconds},"beats":[{{"name":"段落名","shots":3,"seconds":24,"summary":"这一段讲了什么"}}]}}"""


@dataclass
class _DirectorContext:
    plan: dict[str, Any]
    shot_count: int
    shot_seconds: int
    target_seconds: int
    script: str
    issues: list[dict[str, str]] = field(default_factory=list)
    revisions: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


def _text(value: Any) -> str:
    return str(value or "").strip()


def validate_plan(
    plan: dict[str, Any],
    *,
    script: str,
    shot_count: int,
    shot_seconds: int,
    target_seconds: int,
) -> list[dict[str, str]]:
    """确定性校验：只报能算准的结构 / 时长问题，不做语义好坏判断。"""
    issues: list[dict[str, str]] = []

    def add(level: str, code: str, message: str) -> None:
        issues.append({"level": level, "code": code, "message": message})

    if not _text(script):
        add("critical", "script_empty", "剧本为空，无法核对方案是否覆盖情节。")

    beats = plan.get("beats") if isinstance(plan.get("beats"), list) else []
    if not beats:
        add("critical", "beats_empty", "方案没有叙事段落（beats），无法据此拆解。")
    else:
        total = sum(max(0, int(beat.get("shots") or 0)) for beat in beats if isinstance(beat, dict))
        if total != shot_count:
            add(
                "critical",
                "shot_sum",
                f"各段镜头数之和为 {total}，与目标 {shot_count} 不一致；直接拆解会少镜或多镜。",
            )
        for index, beat in enumerate(beats, start=1):
            if not isinstance(beat, dict):
                add("critical", "beat_invalid", f"第 {index} 段不是有效对象。")
                continue
            if int(beat.get("shots") or 0) < 1:
                add("critical", "beat_zero", f"第 {index} 段镜头数为 0，该段会被跳过。")
            if not _text(beat.get("name")):
                add("warn", "beat_name", f"第 {index} 段缺少段落名。")
            if not _text(beat.get("summary")):
                add("warn", "beat_summary", f"第 {index} 段缺少摘要，拆解时容易丢情节。")
        if len(beats) > 6:
            add("warn", "beat_count", f"段落数 {len(beats)} 超过 6 段，叙事会显得碎。")
        if shot_count >= 6 and len(beats) < 3:
            add("warn", "beat_count_low", f"{shot_count} 个镜头只分了 {len(beats)} 段，节奏层次不足。")

    seconds_sum = sum(int(beat.get("seconds") or 0) for beat in beats if isinstance(beat, dict))
    if target_seconds:
        tolerance = max(5, int(target_seconds * 0.15))
        if seconds_sum and abs(seconds_sum - target_seconds) > tolerance:
            add(
                "warn",
                "duration_sum",
                f"各段时长之和为 {seconds_sum} 秒，与目标 {target_seconds} 秒相差超过 {tolerance} 秒。",
            )
    if not 5 <= int(shot_seconds or 0) <= 12:
        add("warn", "shot_seconds", f"单镜时长 {shot_seconds} 秒不在 5-12 秒的稳定区间。")
    return issues


def _has_critical(issues: list[dict[str, str]]) -> bool:
    return any(item.get("level") == "critical" for item in issues)


def _coerce_plan(raw: dict[str, Any], current: dict[str, Any], *, shot_count: int, shot_seconds: int, target_seconds: int) -> dict[str, Any]:
    """把模型修订稿收回到硬约束内：镜头数对不上就按比例缩放，不让错误方案流出去。"""
    normalized = prompts.normalize_plan(raw) or {"reasoning": "", "beats": []}
    beats = [beat for beat in normalized.get("beats") or [] if isinstance(beat, dict)]
    if not beats:
        return current
    planned = sum(int(beat.get("shots") or 0) for beat in beats)
    if planned != shot_count and planned > 0:
        assigned = 0
        scaled: list[dict] = []
        for index, beat in enumerate(beats):
            if index == len(beats) - 1:
                shots = max(1, shot_count - assigned)
            else:
                shots = max(1, round(int(beat.get("shots") or 0) * shot_count / planned))
            assigned += shots
            scaled.append({**beat, "shots": shots})
        beats = scaled
        if sum(int(beat["shots"]) for beat in beats) != shot_count:
            # 末段兜底后仍不齐（极小概率），按差数补到最后一段
            delta = shot_count - sum(int(beat["shots"]) for beat in beats)
            beats[-1]["shots"] = max(1, int(beats[-1]["shots"]) + delta)
    for beat in beats:
        if not int(beat.get("seconds") or 0):
            beat["seconds"] = max(0, int(beat.get("shots") or 0) * shot_seconds)
    return {
        "reasoning": _text(normalized.get("reasoning")) or _text(current.get("reasoning")),
        "shot_count": shot_count,
        "shot_seconds": shot_seconds,
        "target_seconds": target_seconds,
        "beats": beats,
    }


def _plan_json(plan: dict[str, Any]) -> str:
    import json

    return json.dumps(
        {
            "reasoning": plan.get("reasoning") or "",
            "shot_count": plan.get("shot_count"),
            "shot_seconds": plan.get("shot_seconds"),
            "target_seconds": plan.get("target_seconds"),
            "beats": plan.get("beats") or [],
        },
        ensure_ascii=False,
        indent=2,
    )


def _issues_text(issues: list[dict[str, str]]) -> str:
    if not issues:
        return "无"
    return "\n".join(f"- [{item['level']}] {item['message']}" for item in issues)


async def review_plan(
    session: AsyncSession,
    *,
    user_id: int,
    episode: Episode,
    plan: dict[str, Any],
    script: str,
    shot_count: int,
    shot_seconds: int,
    target_seconds: int,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """执行一次导演自检；无问题时 0 次模型调用，有硬伤才进入有界修订循环。"""
    from app.services.generation import _generate_json, build_provider_from, resolve_credential

    ctx = _DirectorContext(
        plan=plan,
        shot_count=shot_count,
        shot_seconds=shot_seconds,
        target_seconds=target_seconds,
        script=script,
    )
    ctx.issues = validate_plan(
        plan,
        script=script,
        shot_count=shot_count,
        shot_seconds=shot_seconds,
        target_seconds=target_seconds,
    )
    if not ctx.issues:
        await record_run(
            session,
            user_id=user_id,
            kind="director",
            status="clean",
            project_id=episode.project_id,
            target_type="episode",
            target_id=episode.id,
            steps=0,
            stop_reason="clean",
            summary={"issues": [], "revisions": 0},
            trace=[],
        )
        return {
            "status": "clean",
            "plan": plan,
            "issues": [],
            "revisions": 0,
            "trace": [],
            "steps": 0,
            "advisory": True,
            "message": "结构校验通过，未产生额外模型调用。",
        }

    credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    provider = build_provider_from(credential)
    decision_model = model or provider.model_for("text")

    async def decide(prompt: str) -> dict[str, Any]:
        return await _generate_json(
            provider,
            prompt,
            system=_SYSTEM,
            model=decision_model,
            temperature=0.2,
            max_tokens=1600,
            attempts=2,
        )

    async def inspect(_: dict[str, Any]) -> runtime.ToolResult:
        lines = [
            f"分集：{_text(episode.title)}",
            f"硬约束：{ctx.shot_count} 镜，单镜约 {ctx.shot_seconds} 秒，目标 {ctx.target_seconds} 秒。",
            "当前方案：",
            _plan_json(ctx.plan),
            "当前校验发现：",
            _issues_text(ctx.issues),
        ]
        return runtime.ToolResult("\n".join(lines))

    async def validate(_: dict[str, Any]) -> runtime.ToolResult:
        ctx.issues = validate_plan(
            ctx.plan,
            script=ctx.script,
            shot_count=ctx.shot_count,
            shot_seconds=ctx.shot_seconds,
            target_seconds=ctx.target_seconds,
        )
        return runtime.ToolResult(
            "重新校验：" + ("无问题" if not ctx.issues else "\n" + _issues_text(ctx.issues)),
            data={"issues": ctx.issues},
        )

    async def revise(_: dict[str, Any]) -> runtime.ToolResult:
        raw = await _generate_json(
            provider,
            _REVISE_PROMPT.format(
                shot_count=ctx.shot_count,
                shot_seconds=ctx.shot_seconds,
                target_seconds=ctx.target_seconds,
                plan_json=_plan_json(ctx.plan),
                issues=_issues_text(ctx.issues),
                script=ctx.script[:12000],
            ),
            system=prompts.SHOT_PLAN_SYSTEM,
            model=decision_model,
            temperature=0.3,
            max_tokens=2048,
            attempts=2,
        )
        revised = _coerce_plan(
            raw,
            ctx.plan,
            shot_count=ctx.shot_count,
            shot_seconds=ctx.shot_seconds,
            target_seconds=ctx.target_seconds,
        )
        if revised == ctx.plan:
            return runtime.ToolResult("修订稿与当前方案相同，请针对校验问题改写。", ok=False)
        ctx.plan = revised
        ctx.revisions += 1
        ctx.history.append({"revision": ctx.revisions, "plan": revised})
        return runtime.ToolResult(
            f"已生成第 {ctx.revisions} 版修订稿：{len(revised.get('beats') or [])} 段，"
            f"{sum(int(beat.get('shots') or 0) for beat in revised.get('beats') or [])} 镜。请重新 validate。",
            data={"revision": ctx.revisions, "plan": revised},
        )

    tools = [
        runtime.AgentTool(
            "inspect", "重新读取方案、剧本约束与校验发现", {}, inspect, kind="read", max_calls=2
        ),
        runtime.AgentTool(
            "validate",
            "运行确定性校验：镜头数之和、段落结构、时长预算",
            {},
            validate,
            kind="read",
            max_calls=3,
        ),
        runtime.AgentTool(
            "revise",
            "按校验发现修订方案（只改段落与镜数，不写分镜正文）",
            {},
            revise,
            kind="expensive",
            max_calls=2,
        ),
    ]

    run = await runtime.run_agent(
        goal=(
            "让拆解方案通过结构校验：镜头数之和等于目标、段落覆盖剧情、时长在预算内；"
            "最多修订 2 次，仍有硬伤就如实标 needs_human。"
        ),
        tools=tools,
        decide=decide,
        initial_observation=(
            f"{_text(episode.title)}\n"
            f"硬约束：{ctx.shot_count} 镜 / 单镜 {ctx.shot_seconds} 秒 / 目标 {ctx.target_seconds} 秒\n"
            f"当前方案：\n{_plan_json(ctx.plan)}\n"
            f"校验发现：\n{_issues_text(ctx.issues)}\n\n{_POLICY}"
        ),
        max_steps=6,
        max_expensive_calls=2,
    )

    # 模型可能没调用 validate 就 finish：结束时强制按最新方案复核一次
    ctx.issues = validate_plan(
        ctx.plan,
        script=ctx.script,
        shot_count=ctx.shot_count,
        shot_seconds=ctx.shot_seconds,
        target_seconds=ctx.target_seconds,
    )
    if not ctx.issues:
        status = "revised" if ctx.revisions else "clean"
    elif _has_critical(ctx.issues):
        status = "needs_human"
    else:
        status = "revised_with_warnings"
    if run.status == "agent_error":
        status = "agent_error"

    result = {
        "status": status,
        "plan": ctx.plan,
        "issues": ctx.issues,
        "revisions": ctx.revisions,
        "trace": run.trace,
        "steps": run.steps,
        "stop_reason": run.stop_reason,
        "advisory": True,
        "model": decision_model,
    }
    if run.status == "agent_error":
        result["error"] = (run.final or {}).get("error")
    logger.info(
        "导演自检完成：episode=%s status=%s steps=%s revisions=%s issues=%s",
        episode.id,
        status,
        run.steps,
        ctx.revisions,
        len(ctx.issues),
    )
    await record_run(
        session,
        user_id=user_id,
        kind="director",
        status=status,
        project_id=episode.project_id,
        target_type="episode",
        target_id=episode.id,
        model=decision_model,
        steps=run.steps,
        stop_reason=run.stop_reason,
        summary={
            "issues": ctx.issues,
            "revisions": ctx.revisions,
            "shot_count": ctx.shot_count,
            "target_seconds": ctx.target_seconds,
        },
        trace=run.trace,
        error=result.get("error"),
    )
    return result
