"""RSI 自动提案：把近期失败与诊断结论聚类，起草一个白名单模板 patch。

只做三件事：
1. 汇总窗口内的失败台账（generation_ledger）与诊断结论（agent_runs, kind=diagnose）；
2. 让文本模型针对指定模板键起草小步改写，并给出引用证据的理由；
3. 走 ``compiler_templates.propose`` 的 16 份金镜头断言，过了也只是进待认可队列。

不自动认可、不写回 .env、不碰项目级资产。证据不足或模型认为无须改动时直接跳过。

    docker compose exec backend python /app/scripts/rsi_propose_from_diagnoses.py \
        --key video_continuity_compact --days 14 --project 49
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.ledger import GenerationLedger
from app.models.project import Project
from app.services import compiler_templates, prompts

_SYSTEM = (
    "你是短剧生成流水线的提示词工程师。你只根据给定的失败事实做小步改写，"
    "不虚构数据、不夹带项目或角色信息。每轮只输出一个 JSON 对象。"
)

_KEY_BRIEFS = {
    "video_continuity_compact": (
        "视频提示词在预算裁剪时替换「前后镜头衔接」整块。"
        "内容必须以【前后镜头衔接】开头，说明本镜如何承接上一镜、如何给下一镜收口；"
        "不得出现具体项目名、角色名或某一条失败日志。"
    ),
    "video_negative_compact": (
        "视频预算吃紧时使用的负面词表。内容是英文逗号分隔的词，不能带块头、不能换行。"
        "必须保留畸形/多手/分身与文字水印两条底线，可补充视频特有的失效模式（中途换场、多出人物）。"
    ),
    "image_negative_compact": (
        "图片预算吃紧时使用的负面词表。内容是英文逗号分隔的词，不能带块头、不能换行。"
        "必须保留形体/多手/分身与画质文字两条底线，可补充图片特有的失效模式。"
    ),
}

_MAX_CHARS = {
    "video_continuity_compact": 160,
    "video_negative_compact": 240,
    "image_negative_compact": 240,
}

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|[0-9a-f]{32,})", re.I)
_DIGITS_RE = re.compile(r"#?\d{2,}")


def _scrub(text: str) -> str:
    return _SECRET_RE.sub("***", text or "")


def _signature(error: str) -> str:
    text = _scrub(error).strip()
    text = _DIGITS_RE.sub("#", text)
    return text[:140] or "（无错误信息）"


def _norm_terms(content: str) -> str:
    """负面词表：去掉误加的块头，换行折成逗号，其余原样交给断言。"""
    text = re.sub(r"^【负面提示[^】]*】\s*", "", (content or "").strip())
    return re.sub(r"\s*\n+\s*", ", ", text).strip()


def _norm_content(key: str, content: str) -> str:
    text = (content or "").strip()
    if key in ("video_negative_compact", "image_negative_compact"):
        return _norm_terms(text)
    return text


async def _pick_user(session: AsyncSession, explicit: int | None) -> int:
    if explicit:
        return explicit
    row = (
        await session.execute(
            select(Project.user_id, func.count(Project.id).label("projects"))
            .group_by(Project.user_id)
            .order_by(func.count(Project.id).desc(), Project.user_id)
            .limit(1)
        )
    ).first()
    if row is None:
        raise SystemExit("数据库里没有项目，无法推断用户；请用 --user 指定。")
    return int(row[0])


async def _collect(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int | None,
    days: int,
) -> tuple[list[GenerationLedger], list[AgentRun], datetime]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(GenerationLedger).where(
        GenerationLedger.user_id == user_id,
        GenerationLedger.status == "failed",
        GenerationLedger.created_at >= cutoff,
    )
    if project_id is not None:
        stmt = stmt.where(GenerationLedger.project_id == project_id)
    failures = list(
        (await session.execute(stmt.order_by(GenerationLedger.id.desc()).limit(200))).scalars()
    )

    runs_stmt = select(AgentRun).where(
        AgentRun.user_id == user_id,
        AgentRun.kind == "diagnose",
        AgentRun.created_at >= cutoff,
    )
    if project_id is not None:
        runs_stmt = runs_stmt.where(AgentRun.project_id == project_id)
    runs = list(
        (await session.execute(runs_stmt.order_by(AgentRun.id.desc()).limit(60))).scalars()
    )
    return failures, runs, cutoff


def _failure_digest(failures: list[GenerationLedger]) -> str:
    groups: Counter = Counter()
    for row in failures:
        key = (row.kind, row.provider or "-", row.model or "-", _signature(row.error or ""))
        groups[key] += 1

    lines = [
        f"窗口内失败任务 {len(failures)} 条，按「类型 / 厂商 / 模型 / 错误」聚合："
    ]
    for (kind, provider, model, sig), count in groups.most_common(10):
        lines.append(f"- {count}× {kind} {provider}/{model}：{sig}")
    if not failures:
        lines.append("- （无失败任务）")
    return "\n".join(lines)


def _diagnosis_digest(runs: list[AgentRun]) -> str:
    categories: Counter = Counter()
    suggestions: list[str] = []
    for run in runs:
        summary = run.summary if isinstance(run.summary, dict) else {}
        diagnosis = summary.get("diagnosis") if isinstance(summary.get("diagnosis"), dict) else {}
        category = str(diagnosis.get("category") or "unknown")
        categories[category] += 1
        suggestion = str(diagnosis.get("suggestion") or "").strip()
        if suggestion and suggestion not in suggestions:
            suggestions.append(suggestion[:180])
    lines = [f"窗口内诊断 {len(runs)} 条，分类分布："]
    if categories:
        lines.append(
            "；".join(f"{name}={count}" for name, count in categories.most_common())
        )
    else:
        lines.append("- （无诊断记录）")
    if suggestions:
        lines.append("既有建议（去重，最多 5 条）：")
        lines.extend(f"- {item}" for item in suggestions[:5])
    return "\n".join(lines)


def _build_prompt(
    *,
    key: str,
    baseline: str,
    failures_text: str,
    diagnoses_text: str,
    days: int,
    project_id: int | None,
) -> str:
    scope = f"项目 {project_id}" if project_id is not None else "该用户全部项目"
    return f"""为编译模板 `{key}` 起草一个 patch。

模板作用：{_KEY_BRIEFS[key]}

当前生效内容（基线）：
{baseline}
基线长度：{len(baseline)} 字；本键硬上限 {_MAX_CHARS[key]} 字。

统计口径：{scope}，最近 {days} 天。

【失败台账】
{failures_text}

【诊断结论】
{diagnoses_text}

要求：
1. 只做小步改写（改措辞或补一两条仍属本模板职责的约束），不要重写成完全不同的东西；
2. 只依据上面的事实，禁止虚构失败原因，禁止写具体项目/角色/任务编号；
3. 若这些失败与 `{key}` 的职责无关，或证据不足以支撑改动，content 输出空字符串；
4. 只输出 JSON：{{"content": "改动后的完整模板内容", "rationale": "引用上面的证据说明为什么改"}}。
"""


async def _draft(
    session: AsyncSession,
    *,
    user_id: int,
    prompt: str,
    credential_id: int | None,
    provider_key: str | None,
    model: str | None,
) -> dict[str, Any]:
    from app.services.generation import _generate_json, build_provider_from, resolve_credential

    credential = await resolve_credential(session, user_id, "text", credential_id, provider_key)
    provider = build_provider_from(credential)
    return await _generate_json(
        provider,
        prompt,
        system=_SYSTEM,
        model=model or provider.model_for("text"),
        temperature=0.3,
        max_tokens=2048,
        attempts=2,
    )


async def main(args: argparse.Namespace) -> int:
    async with compiler_session() as session:
        user_id = await _pick_user(session, args.user)
        failures, runs, cutoff = await _collect(
            session, user_id=user_id, project_id=args.project, days=args.days
        )
        failures_text = _failure_digest(failures)
        diagnoses_text = _diagnosis_digest(runs)
        active = await compiler_templates.load_active(session)
        baseline = active.get(args.key) or prompts.template_content(args.key)

        print(
            f"用户 {user_id}；窗口起点 {cutoff:%Y-%m-%d %H:%M} UTC；"
            f"失败 {len(failures)} 条；诊断 {len(runs)} 条；"
            f"当前基线={'已认可覆盖' if args.key in active else '内置默认'}（{len(baseline)} 字）"
        )
        print(failures_text)
        print(diagnoses_text)

        if not failures and not runs:
            print("SKIP: 窗口内没有失败或诊断证据，不提案。")
            return 0
        if args.dry_run:
            print("DRY-RUN: 只汇总，不调用模型、不入库。")
            return 0
        if len(failures) < args.min_failures:
            print(
                f"SKIP: 失败样本 {len(failures)} 条 < --min-failures {args.min_failures}，"
                "单独一条错误不足以支撑模板改动。"
            )
            return 0

        if not baseline:
            print(f"SKIP: 未知模板键 {args.key}")
            return 1

        prompt = _build_prompt(
            key=args.key,
            baseline=baseline,
            failures_text=failures_text,
            diagnoses_text=diagnoses_text,
            days=args.days,
            project_id=args.project,
        )
        try:
            draft = await _draft(
                session,
                user_id=user_id,
                prompt=prompt,
                credential_id=args.credential_id,
                provider_key=args.provider,
                model=args.model,
            )
        except Exception as exc:  # noqa: BLE001 - CLI 要给出可读原因
            print(f"FAILED: 起草失败：{type(exc).__name__}: {_scrub(str(exc))[:300]}")
            return 1

        content = _norm_content(args.key, str(draft.get("content") or ""))
        rationale = _scrub(str(draft.get("rationale") or "")).strip()[:500]
        if not content:
            print("NO_CHANGE: 模型判断现有证据不足以改动该模板。")
            if rationale:
                print(f"理由：{rationale}")
            return 0
        if len(content) > _MAX_CHARS[args.key]:
            print(f"REJECTED: 草稿 {len(content)} 字超过本键上限 {_MAX_CHARS[args.key]} 字。")
            return 1
        if content.strip() == baseline.strip():
            print("NO_CHANGE: 草稿与当前基线一致。")
            return 0

        print("草稿：")
        print(content)
        print(f"理由：{rationale or '（模型未给出）'}")

        try:
            row = await compiler_templates.propose(
                session,
                user_id=user_id,
                key=args.key,
                content=content,
                rationale=rationale or None,
            )
        except ValueError as exc:
            print(f"REJECTED: {exc}")
            return 1
        await session.commit()
        print(
            f"PROPOSED: id={row.id} key={row.key} version={row.version} "
            f"checked={row.validated.get('checked') if row.validated else '?'}"
        )
        print("仍未生效：人工认可后才会装载。POST /api/compiler-templates/{id}/approve")
        return 0


def compiler_session():
    from app.db.session import AsyncSessionLocal

    return AsyncSessionLocal()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True, choices=prompts.TEMPLATE_KEYS)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--project", type=int, default=None)
    parser.add_argument("--user", type=int, default=None)
    parser.add_argument("--credential-id", type=int, default=None)
    parser.add_argument("--provider", default=None, help="指定文本厂商（默认按偏好顺序）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--min-failures", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="只打印汇总，不调模型不入库")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args)))
