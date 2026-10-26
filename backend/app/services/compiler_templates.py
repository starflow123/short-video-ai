"""全局编译模板的版本管理：RSI 的护栏核心。

提案 → 金镜头断言验证 → 人工认可 → 生效；rollback 回到上一个已认可版本。
任何未经认可的模板内容永远不会进入提示词编译器。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compiler_template import CompilerTemplate
from app.services import prompt_checks
from app.services.prompts import TEMPLATE_KEYS, set_template_overrides


async def load_active(session: AsyncSession) -> dict[str, str]:
    """当前生效的模板覆盖（key → content），启动与认可后调用。"""
    rows = (
        await session.execute(
            select(CompilerTemplate).where(
                CompilerTemplate.is_active == True,  # noqa: E712
                CompilerTemplate.approved_at.is_not(None),
            )
        )
    ).scalars().all()
    return {row.key: row.content for row in rows if row.key in TEMPLATE_KEYS}


async def propose(
    session: AsyncSession,
    *,
    user_id: int,
    key: str,
    content: str,
    rationale: str | None = None,
    proposed_by_run_id: int | None = None,
) -> CompilerTemplate:
    """提交一个模板 patch：必须先过全部金镜头断言，否则直接拒绝入队。"""
    if key not in TEMPLATE_KEYS:
        raise ValueError(f"模板键 {key} 不在白名单里（允许：{', '.join(TEMPLATE_KEYS)}）")
    content = str(content or "").strip()
    if not content:
        raise ValueError("模板内容不能为空")

    outcome = prompt_checks.validate_all_snapshots(template_overrides={key: content})
    if outcome["checked"] == 0:
        # 镜像里丢了 fixtures 时断言会「全过」，那等于没有护栏
        raise ValueError("没有找到金镜头快照，拒绝入队（检查 tests/fixtures/prompt_snapshots）")
    if not outcome["passed"]:
        raise ValueError("金镜头断言未通过，拒绝入队：\n" + "\n".join(outcome["violations"][:8]))

    max_version = (
        await session.execute(
            select(CompilerTemplate.version)
            .where(CompilerTemplate.key == key)
            .order_by(CompilerTemplate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or 0
    row = CompilerTemplate(
        user_id=user_id,
        key=key,
        content=content,
        version=max_version + 1,
        is_active=False,
        rationale=rationale,
        validated={"checked": outcome["checked"], "violations": []},
        proposed_by_run_id=proposed_by_run_id,
    )
    session.add(row)
    await session.flush()
    return row


async def approve(
    session: AsyncSession,
    *,
    user_id: int,
    template_id: int,
) -> CompilerTemplate:
    """人工认可：候选版本转正，同 key 的旧 active 退位（保留可回滚）。"""
    row = await session.get(CompilerTemplate, template_id)
    if row is None or row.user_id != user_id:
        raise ValueError("模板版本不存在")
    if row.validated is None:
        raise ValueError("该版本没跑过金镜头断言，不能认可（先走 propose 入队）")
    now = datetime.now(timezone.utc)
    await session.execute(
        update(CompilerTemplate)
        .where(CompilerTemplate.key == row.key, CompilerTemplate.is_active == True)  # noqa: E712
        .values(is_active=False)
    )
    row.is_active = True
    row.approved_at = row.approved_at or now
    await session.flush()

    overrides = await load_active(session)
    set_template_overrides(overrides)
    return row


async def rollback(
    session: AsyncSession,
    *,
    user_id: int,
    key: str,
) -> CompilerTemplate | None:
    """回滚：当前 active 退位，回到它之前最近的已认可版本；没有就回到内置模板。"""
    current = (
        await session.execute(
            select(CompilerTemplate)
            .where(
                CompilerTemplate.user_id == user_id,
                CompilerTemplate.key == key,
                CompilerTemplate.is_active == True,  # noqa: E712
            )
        )
    ).scalars().first()
    if current is None:
        raise ValueError(f"{key} 当前没有生效的覆盖版本")
    previous = (
        await session.execute(
            select(CompilerTemplate)
            .where(
                CompilerTemplate.user_id == user_id,
                CompilerTemplate.key == key,
                CompilerTemplate.id != current.id,
                CompilerTemplate.approved_at.is_not(None),
            )
            .order_by(CompilerTemplate.version.desc())
            .limit(1)
        )
    ).scalars().first()
    current.is_active = False
    if previous is not None:
        previous.is_active = True
    await session.flush()

    overrides = await load_active(session)
    set_template_overrides(overrides)
    return previous
