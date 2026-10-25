"""Celery 任务定义。

Celery worker 是同步上下文，这里用一个常驻事件循环桥接异步编排逻辑。

注意：不能每个任务都调用 ``asyncio.run``——它会新建并关闭事件循环，
而模块级的 SQLAlchemy 异步引擎连接池绑定在首个循环上，
换循环后会抛 ``attached to a different loop`` / ``Event loop is closed``。
"""
from __future__ import annotations

import asyncio

from app.db.session import AsyncSessionLocal
from app.services.generation import execute_task, reap_stale_tasks
from app.worker.celery_app import celery_app

_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


@celery_app.task(name="tasks.execute_generation", bind=True)
def execute_generation(self, task_id: int) -> None:  # noqa: ANN001 - Celery 注入 self
    _get_loop().run_until_complete(execute_task(task_id))


async def _reap_stale() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        return await reap_stale_tasks(session)


@celery_app.task(name="tasks.reap_stale")
def reap_stale() -> dict[str, int]:
    return _get_loop().run_until_complete(_reap_stale())


def enqueue_generation(task_id: int) -> str:
    """投递生成任务，返回 Celery 任务 ID。"""
    async_result = execute_generation.apply_async(args=[task_id])
    return async_result.id
