"""失败诊断 agent：读失败任务 → 查项目与密钥 → 给出可执行结论（可选换密钥重试）。

设计边界：
- 默认只诊断、不重试（``allow_retry=False``），失败任务本身不会因此改变状态；
- 工具只读为主，唯一的写操作是「用另一套凭证重跑一次」，需要显式开启；
- 诊断失败、没有文本密钥时如实转人工，不编造原因；
- 结论写进 ``agent_runs``（kind=diagnose），可在任务详情里回看。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.credential import ProviderCredential
from app.models.episode import Episode
from app.models.shot import Shot
from app.models.task import GenerationTask
from app.services.agents import runtime
from app.services.agents.audit import record_run
from app.services.providers.base import ProviderError

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是短剧生成流水线的排障工程师。你只能根据工具返回的事实判断失败原因，"
    "不猜测、不编造日志。每轮只输出一个 JSON："
    '{"thought":"...","action":"工具名","args":{...}}。'
)

_POLICY = (
    "诊断策略：\n"
    "1. 先 inspect_task 看失败任务本身的错误、类型与参数；再看 inspect_credentials 判断是否密钥/能力问题。\n"
    "2. 错误涉及具体镜头、文件、流水线时，用 inspect_project 看项目里其它任务与镜头状态。\n"
    "3. 只有证据指向「当前凭证不可用 / 限额 / 鉴权失败」，且允许重试时，才调用 retry_task 换一套凭证重跑。\n"
    "4. 结论必须落到 report：category、evidence（引用工具返回的具体事实）、suggestion（用户下一步该做什么）。\n"
    "5. 证据不足时 category=unknown 并说明缺什么，不要硬猜；不要把「生成质量差」当成任务失败。"
)

_CATEGORIES = {
    "credential_missing",
    "auth",
    "quota",
    "timeout",
    "content_rejected",
    "asset_missing",
    "config",
    "provider_error",
    "unknown",
}

_CATEGORY_LABELS = {
    "credential_missing": "缺少可用密钥 / 能力未勾选",
    "auth": "密钥无效或无权限",
    "quota": "限流或额度不足",
    "timeout": "网络超时或厂商响应慢",
    "content_rejected": "内容审核拒绝或提示词被拒",
    "asset_missing": "素材 / 文件缺失",
    "config": "参数缺失或任务类型不支持",
    "provider_error": "厂商服务异常",
    "unknown": "证据不足，需人工确认",
}

# 任务类型 → 重跑所需的厂商能力
_KIND_CAPABILITY = {
    "text": "text",
    "image": "image",
    "video": "video",
    "repair": "text",
    "diagnose": "text",
    "cut_repair": "text",
}

# 重跑时不能带上的链字段：原链已收敛，带上会重复推进后续镜头
_CHAIN_KEYS = ("flow", "chain_next", "run_id")


@dataclass
class _DiagnosisContext:
    failed_task_id: int
    kind: str
    provider: str | None
    model: str | None
    error: str
    project_id: int | None
    shot_id: int | None
    episode_id: int | None
    params: dict[str, Any]
    allow_retry: bool
    retry_credential_id: int | None
    retry_provider_key: str | None
    retry_model: str | None
    report: dict[str, Any] | None = None
    retry_task_id: int | None = None
    retry_detail: str = ""
    notes: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _short(value: Any, limit: int) -> str:
    text = _text(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _describe_params(params: dict[str, Any]) -> str:
    """只列与排障有关的字段，提示词截断，任何密钥类字段都不出现。"""
    if not params:
        return "（无参数）"
    keys = [
        "shot_id",
        "episode_id",
        "keyframe_id",
        "asset_id",
        "flow",
        "run_id",
        "auto_images",
        "count",
        "n",
        "duration",
        "resolution",
        "aspect_ratio",
        "provider",
        "model",
        "credential_id",
    ]
    lines: list[str] = []
    for key in keys:
        if key in params and params[key] not in (None, "", [], {}):
            lines.append(f"{key}={_short(params[key], 80)}")
    for key in ("prompt", "image_prompt", "video_prompt", "negative_prompt"):
        if _text(params.get(key)):
            lines.append(f"{key}={_short(params.get(key), 160)}")
    return "；".join(lines) or "（无参数）"


async def _load_failed(session: AsyncSession, task_id: int) -> GenerationTask:
    task = await session.get(GenerationTask, task_id)
    if task is None:
        raise ProviderError("失败任务不存在")
    return task


def _snapshot(task: GenerationTask, *, allow_retry: bool, retry_credential_id: int | None,
              retry_provider_key: str | None, retry_model: str | None) -> _DiagnosisContext:
    return _DiagnosisContext(
        failed_task_id=int(task.id),
        kind=_text(task.kind),
        provider=task.provider,
        model=task.model,
        error=_text(task.error) or "（任务未记录错误信息）",
        project_id=task.project_id,
        shot_id=task.shot_id,
        episode_id=task.episode_id,
        params=dict(task.params or {}),
        allow_retry=allow_retry,
        retry_credential_id=retry_credential_id,
        retry_provider_key=retry_provider_key,
        retry_model=retry_model,
    )


async def run_diagnosis(
    session: AsyncSession,
    *,
    user_id: int,
    failed_task_id: int,
    allow_retry: bool = False,
    credential_id: int | None = None,
    provider_key: str | None = None,
    model: str | None = None,
    retry_credential_id: int | None = None,
    retry_provider_key: str | None = None,
    retry_model: str | None = None,
) -> dict[str, Any]:
    """执行一次失败诊断；默认只给建议，不重跑。"""
    from app.services.generation import _generate_json, build_provider_from, resolve_credential
    from app.services.dispatch import create_task

    failed = await _load_failed(session, failed_task_id)
    ctx = _snapshot(
        failed,
        allow_retry=allow_retry,
        retry_credential_id=retry_credential_id,
        retry_provider_key=retry_provider_key,
        retry_model=retry_model,
    )

    try:
        credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    except ProviderError as exc:
        # 连诊断用的文本密钥都没有：如实返回，任务本身不算失败
        logger.warning("任务 %s 诊断缺少文本凭证：%s", failed_task_id, exc)
        result = {
            "action": "diagnose",
            "status": "review_unavailable",
            "target_task_id": failed_task_id,
            "diagnosis": {
                "category": "credential_missing",
                "confidence": "high",
                "evidence": [str(exc)[:200]],
                "suggestion": "到「密钥管理」添加一个勾选「文本」能力的 API Key，再重新诊断。",
                "retry_recommended": False,
            },
            "allow_retry": allow_retry,
            "steps": 0,
            "trace": [],
            "advisory": True,
            "message": f"无法使用文本模型做诊断：{exc}",
        }
        await record_run(
            session,
            user_id=user_id,
            kind="diagnose",
            status="review_unavailable",
            project_id=ctx.project_id,
            target_type="task",
            target_id=failed_task_id,
            summary={"diagnosis": result["diagnosis"], "error": str(exc)[:200]},
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
            temperature=0.1,
            max_tokens=1200,
            attempts=2,
        )

    async def inspect_task(_: dict[str, Any]) -> runtime.ToolResult:
        lines = [
            f"任务 {ctx.failed_task_id}：kind={ctx.kind}，provider={ctx.provider or '未指定'}，"
            f"model={ctx.model or '未指定'}",
            f"错误：{_short(ctx.error, 1200)}",
            f"参数：{_describe_params(ctx.params)}",
        ]
        if ctx.shot_id:
            shot = await session.get(Shot, ctx.shot_id)
            if shot is not None:
                lines.append(
                    f"镜头：第 {shot.order_index} 镜《{_text(shot.title) or '未命名'}》，"
                    f"状态={shot.status}，视频={'有' if shot.video_media_id else '无'}"
                )
        if ctx.episode_id:
            episode = await session.get(Episode, ctx.episode_id)
            if episode is not None:
                lines.append(f"分集：《{_text(episode.title)}》，状态={episode.status}")
        if ctx.params.get("prompt"):
            lines.append(f"当时提示词：{_short(ctx.params.get('prompt'), 600)}")
        return runtime.ToolResult("\n".join(lines))

    async def inspect_project(_: dict[str, Any]) -> runtime.ToolResult:
        if not ctx.project_id:
            return runtime.ToolResult("该任务没有关联项目，无法查看项目内其它任务。")
        failed_rows = list(
            (
                await session.execute(
                    select(GenerationTask)
                    .where(
                        GenerationTask.project_id == ctx.project_id,
                        GenerationTask.status == "failed",
                    )
                    .order_by(desc(GenerationTask.id))
                    .limit(8)
                )
            ).scalars()
        )
        lines = [f"项目 {ctx.project_id} 最近的失败任务（最多 8 条）："]
        if not failed_rows:
            lines.append("- 没有其它失败任务，问题更可能只在这一条任务上。")
        for row in failed_rows:
            lines.append(
                f"- #{row.id} {row.kind} {row.provider or '-'}/{row.model or '-'}："
                f"{_short(row.error, 160)}"
            )
        if ctx.episode_id:
            shot_rows = list(
                (
                    await session.execute(
                        select(Shot).where(Shot.episode_id == ctx.episode_id).order_by(Shot.order_index)
                    )
                ).scalars()
            )
            counts: dict[str, int] = {}
            for shot in shot_rows:
                counts[shot.status] = counts.get(shot.status, 0) + 1
            lines.append(
                "本集镜头状态："
                + "；".join(f"{key}={value}" for key, value in sorted(counts.items()))
            )
        return runtime.ToolResult("\n".join(lines))

    async def inspect_credentials(_: dict[str, Any]) -> runtime.ToolResult:
        capability = _KIND_CAPABILITY.get(ctx.kind)
        rows = list(
            (
                await session.execute(
                    select(ProviderCredential)
                    .where(ProviderCredential.user_id == user_id)
                    .order_by(ProviderCredential.id)
                )
            ).scalars()
        )
        lines = [f"当前任务需要能力：{capability or '（导出类任务不需要厂商能力）'}"]
        for row in rows:
            caps = ",".join(sorted(row.capability_set())) or "（未勾选）"
            check = (
                "未测过"
                if row.last_check_ok is None
                else ("最近一次检测通过" if row.last_check_ok else f"最近一次检测失败：{_short(row.last_check_message, 100)}")
            )
            mark = " ← 当前任务使用" if (ctx.provider and row.provider == ctx.provider) else ""
            lines.append(
                f"- #{row.id} {row.provider}（{_text(row.name)}）能力={caps}，{check}{mark}"
            )
        usable = [row for row in rows if capability and capability in row.capability_set()]
        lines.append(f"可复用于重跑的凭证：{len(usable)} 个。")
        return runtime.ToolResult("\n".join(lines))

    async def retry_task(_: dict[str, Any]) -> runtime.ToolResult:
        if not ctx.allow_retry:
            return runtime.ToolResult(
                "本次诊断未开启重试，只给建议；用户可点「按建议重跑」再执行。", ok=False
            )
        if ctx.kind == "export":
            return runtime.ToolResult("导出任务不需要厂商凭证，请用普通「重试」重新导出。", ok=False)
        capability = _KIND_CAPABILITY.get(ctx.kind, "text")
        try:
            credential_row = await resolve_credential(
                session,
                user_id,
                capability,
                ctx.retry_credential_id,
                ctx.retry_provider_key,
            )
        except ProviderError as exc:
            return runtime.ToolResult(f"没有可用的重跑凭证：{exc}", ok=False)

        original = await session.get(GenerationTask, ctx.failed_task_id)
        if original is None:
            return runtime.ToolResult("失败任务已不存在，无法重跑。", ok=False)
        params = {
            key: value
            for key, value in (original.params or {}).items()
            if key not in _CHAIN_KEYS
        }
        params.update(
            {
                "credential_id": credential_row.id,
                "provider": credential_row.provider,
                "model": ctx.retry_model or original.model,
                "diagnosis_of": ctx.failed_task_id,
            }
        )
        try:
            new_task = await create_task(
                session,
                user_id=user_id,
                kind=original.kind,
                provider=credential_row.provider,
                model=params.get("model"),
                project_id=original.project_id,
                episode_id=original.episode_id,
                shot_id=original.shot_id,
                params=params,
            )
        except Exception as exc:  # noqa: BLE001 - 队列不可用时作为观察返回
            return runtime.ToolResult(f"重跑任务创建失败：{exc}", ok=False)

        ctx.retry_task_id = int(new_task.id)
        ctx.retry_detail = f"已用 {credential_row.provider} 的凭证创建重跑任务 #{new_task.id}"
        if credential_row.provider == ctx.provider:
            ctx.notes.append("重跑仍使用原厂商，可能再次触发同一错误")
        return runtime.ToolResult(
            f"{ctx.retry_detail}；它是一条独立任务，不推进原链。"
            + ("注意：重跑仍使用原厂商。" if credential_row.provider == ctx.provider else ""),
            data={"retry_task_id": new_task.id, "provider": credential_row.provider},
        )

    async def report(args: dict[str, Any]) -> runtime.ToolResult:
        category = _text(args.get("category")).lower()
        if category not in _CATEGORIES:
            category = "unknown"
        confidence = _text(args.get("confidence")).lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        raw_evidence = args.get("evidence")
        if isinstance(raw_evidence, str):
            raw_evidence = [raw_evidence]
        evidence = [
            _short(item, 240)
            for item in (raw_evidence if isinstance(raw_evidence, list) else [])
            if _text(item)
        ][:6]
        suggestion = _short(args.get("suggestion"), 500)
        if not suggestion:
            return runtime.ToolResult("suggestion 不能为空：请写清用户下一步该做什么。", ok=False)
        ctx.report = {
            "category": category,
            "category_label": _CATEGORY_LABELS[category],
            "confidence": confidence,
            "evidence": evidence,
            "suggestion": suggestion,
            "retry_recommended": bool(args.get("retry_recommended")),
        }
        if ctx.notes:
            ctx.report["notes"] = ctx.notes
        if ctx.retry_task_id:
            ctx.report["retry_task_id"] = ctx.retry_task_id
        status = "retried" if ctx.retry_task_id else "diagnosed"
        return runtime.ToolResult(
            f"诊断已记录：{_CATEGORY_LABELS[category]}，置信度 {confidence}。",
            data={"diagnosis": ctx.report, "retry_task_id": ctx.retry_task_id},
            done=True,
            status=status,
        )

    async def escalate(args: dict[str, Any]) -> runtime.ToolResult:
        reason = _short(args.get("reason"), 500) or "证据不足，需要人工排查"
        ctx.report = {
            "category": "unknown",
            "category_label": _CATEGORY_LABELS["unknown"],
            "confidence": "low",
            "evidence": [],
            "suggestion": reason,
            "retry_recommended": False,
            "note": "agent 主动转人工",
        }
        return runtime.ToolResult(
            f"已转人工：{reason}",
            data={"diagnosis": ctx.report},
            done=True,
            status="needs_human",
        )

    tools = [
        runtime.AgentTool(
            "inspect_task",
            "读取失败任务本身的错误、类型、参数与关联镜头/分集",
            {},
            inspect_task,
            kind="read",
            max_calls=2,
        ),
        runtime.AgentTool(
            "inspect_project",
            "查看同一项目下其它失败任务与本集镜头状态",
            {},
            inspect_project,
            kind="read",
            max_calls=2,
        ),
        runtime.AgentTool(
            "inspect_credentials",
            "列出用户凭证的能力与最近检测结果（不显示密钥）",
            {},
            inspect_credentials,
            kind="read",
            max_calls=2,
        ),
        runtime.AgentTool(
            "retry_task",
            "用另一套凭证重跑一次失败任务（会真实产生费用，需用户开启）",
            {},
            retry_task,
            kind="expensive",
            max_calls=1,
        ),
        runtime.AgentTool(
            "report",
            "记录最终诊断。必填 category（credential_missing/auth/quota/timeout/"
            "content_rejected/asset_missing/config/provider_error/unknown）与 suggestion；"
            "可选 confidence（high/medium/low）、evidence（来自工具返回的事实，最多 6 条）、"
            "retry_recommended（true/false）",
            {
                "category": "失败分类",
                "suggestion": "用户下一步该做什么",
            },
            report,
            kind="write",
            max_calls=1,
        ),
        runtime.AgentTool(
            "escalate",
            "证据不足或需要人工处理时结束诊断",
            {"reason": "为什么需要人工"},
            escalate,
            kind="read",
            max_calls=1,
        ),
    ]

    run = await runtime.run_agent(
        goal=(
            "查明这条任务失败的根因，给出用户能直接执行的下一步；"
            "只有证据明确且允许时才换凭证重跑一次。"
        ),
        tools=tools,
        decide=decide,
        initial_observation=(
            f"失败任务 #{ctx.failed_task_id}（{ctx.kind}）\n"
            f"错误：{_short(ctx.error, 1200)}\n"
            f"参数：{_describe_params(ctx.params)}\n"
            f"允许重跑：{'是' if allow_retry else '否'}\n\n{_POLICY}"
        ),
        max_steps=6,
        max_expensive_calls=1,
    )

    summary_text = _short((run.final or {}).get("summary"), 600)
    if ctx.report is None:
        ctx.report = {
            "category": "unknown",
            "category_label": _CATEGORY_LABELS["unknown"],
            "confidence": "low",
            "evidence": [],
            "suggestion": summary_text or "agent 未给出结论，请查看执行轨迹后人工排查。",
            "retry_recommended": False,
        }
    if run.status in ("agent_error",):
        status = "agent_error"
    elif ctx.retry_task_id:
        status = "retried"
    elif run.status in ("needs_human", "escalated"):
        status = "needs_human"
    else:
        status = "diagnosed"

    result = {
        "action": "diagnose",
        "status": status,
        "target_task_id": ctx.failed_task_id,
        "diagnosis": ctx.report,
        "retry_task_id": ctx.retry_task_id,
        "retry_detail": ctx.retry_detail,
        "allow_retry": allow_retry,
        "model": decision_model,
        "steps": run.steps,
        "stop_reason": run.stop_reason,
        "trace": run.trace,
        "advisory": True,
    }
    if run.status == "agent_error":
        result["error"] = (run.final or {}).get("error")
    logger.info(
        "失败诊断完成：task=%s status=%s category=%s steps=%s retry=%s",
        ctx.failed_task_id,
        status,
        ctx.report["category"],
        run.steps,
        ctx.retry_task_id,
    )
    await record_run(
        session,
        user_id=user_id,
        kind="diagnose",
        status=status,
        project_id=ctx.project_id,
        target_type="task",
        target_id=ctx.failed_task_id,
        model=decision_model,
        steps=run.steps,
        stop_reason=run.stop_reason,
        summary={
            "diagnosis": ctx.report,
            "retry_task_id": ctx.retry_task_id,
            "allow_retry": allow_retry,
        },
        trace=run.trace,
        error=result.get("error"),
    )
    return result


async def maybe_diagnose_failed_task(session: AsyncSession, task_id: int) -> None:
    """失败后自动挂一条诊断任务；开关关闭、近期已诊断过或队列异常时静默跳过。"""
    from app.core.config import settings
    from app.services.dispatch import create_task

    if not settings.agent_diagnosis_enabled:
        return
    task = await session.get(GenerationTask, task_id)
    if task is None or task.status != "failed" or not _text(task.error):
        return
    if task.kind in {"diagnose", "repair", "cut_repair"}:
        return
    # 同一项目 10 分钟内只自动诊断一次，避免批量失败时每条都烧一次文本调用
    stmt = select(GenerationTask).where(
        GenerationTask.user_id == task.user_id,
        GenerationTask.kind == "diagnose",
        GenerationTask.status.in_(("pending", "running")),
    )
    if task.project_id is not None:
        stmt = stmt.where(GenerationTask.project_id == task.project_id)
    recent = (await session.execute(stmt.limit(1))).scalars().first()
    if recent is not None:
        logger.info("项目已有进行中的诊断任务 #%s，跳过自动诊断", recent.id)
        return

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    done_stmt = select(AgentRun.id).where(
        AgentRun.user_id == task.user_id,
        AgentRun.kind == "diagnose",
        AgentRun.created_at >= cutoff,
    )
    if task.project_id is not None:
        done_stmt = done_stmt.where(AgentRun.project_id == task.project_id)
    if (await session.execute(done_stmt.limit(1))).first() is not None:
        logger.info("项目 %s 10 分钟内已诊断过，跳过自动诊断", task.project_id)
        return
    try:
        await create_task(
            session,
            user_id=task.user_id,
            kind="diagnose",
            project_id=task.project_id,
            episode_id=task.episode_id,
            shot_id=task.shot_id,
            params={"failed_task_id": task.id, "allow_retry": False, "auto": True},
        )
        logger.info("已为失败任务 #%s 创建自动诊断任务", task.id)
    except Exception as exc:  # noqa: BLE001 - 自动诊断不能影响失败处理
        logger.warning("任务 %s 自动诊断创建失败：%s", task_id, exc)
