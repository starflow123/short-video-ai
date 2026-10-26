"""Celery 任务定义。

Celery worker 是同步上下文，这里用一个常驻事件循环桥接异步编排逻辑。

注意：不能每个任务都调用 ``asyncio.run``——它会新建并关闭事件循环，
而模块级的 SQLAlchemy 异步引擎连接池绑定在首个循环上，
换循环后会抛 ``attached to a different loop`` / ``Event loop is closed``。
"""
from __future__ import annotations

import asyncio

from celery.signals import worker_process_init

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


@worker_process_init.connect
def load_compiler_overrides(**_kwargs) -> None:  # noqa: ANN003 - Celery 信号参数
    """子进程启动时装载已认可的模板覆盖；真正编译生成提示词的是 worker，不加载等于 RSI 不生效。"""

    async def _load() -> None:
        from app.services.compiler_templates import load_active
        from app.services.prompts import set_template_overrides

        async with AsyncSessionLocal() as session:
            overrides = await load_active(session)
        set_template_overrides(overrides)

    try:
        _get_loop().run_until_complete(_load())
    except Exception:  # noqa: BLE001 - 模板覆盖是增强能力
        pass


@celery_app.task(name="tasks.execute_generation", bind=True)
def execute_generation(self, task_id: int) -> None:  # noqa: ANN001 - Celery 注入 self
    _get_loop().run_until_complete(execute_task(task_id))


async def _reap_stale() -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        return await reap_stale_tasks(session)


@celery_app.task(name="tasks.reap_stale")
def reap_stale() -> dict[str, int]:
    return _get_loop().run_until_complete(_reap_stale())


async def _sweep_knowledge() -> int:
    from app.services.knowledge import stale_sweep

    async with AsyncSessionLocal() as session:
        swept = await stale_sweep(session)
        await session.commit()
        return swept


@celery_app.task(name="tasks.sweep_knowledge")
def sweep_knowledge() -> int:
    """把长期未被确认/使用的教训降为 stale（不删，人工 confirm 可复活）。"""
    return _get_loop().run_until_complete(_sweep_knowledge())


def enqueue_generation(task_id: int) -> str:
    """投递生成任务，返回 Celery 任务 ID。"""
    async_result = execute_generation.apply_async(args=[task_id])
    return async_result.id
