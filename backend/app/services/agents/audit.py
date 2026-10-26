"""agent 运行审计：统一落 agent_runs，失败不影响主流程。"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)


async def record_run(
    session: AsyncSession,
    *,
    user_id: int,
    kind: str,
    status: str | None,
    project_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    model: str | None = None,
    steps: int = 0,
    stop_reason: str | None = None,
    summary: dict[str, Any] | None = None,
    trace: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    """只 add + flush，由调用方决定何时 commit；审计写失败只记日志。"""
    try:
        session.add(
            AgentRun(
                user_id=user_id,
                project_id=project_id,
                kind=kind,
                target_type=target_type,
                target_id=target_id,
                status=str(status)[:32] if status else None,
                model=model,
                steps=int(steps or 0),
                stop_reason=stop_reason,
                summary=summary,
                trace=trace,
                error=(error or "")[:2000] or None,
                advisory=True,
            )
        )
        await session.flush()
    except Exception as exc:  # noqa: BLE001 - 审计不能拖垮业务
        logger.warning("agent 审计写入失败（kind=%s target=%s）：%s", kind, target_id, exc)
