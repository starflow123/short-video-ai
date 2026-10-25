"""Celery 应用。"""
from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "ai_video_studio",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    worker_max_tasks_per_child=50,
    # worker 崩溃时已领取的任务要回队列，否则任务永远停在 running
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=3600,
    task_soft_time_limit=3540,
    # 巡检僵尸任务：worker 被杀、队列消息丢失时兜底收敛状态
    beat_schedule={
        "reap-stale-tasks": {
            "task": "tasks.reap_stale",
            "schedule": 300.0,
        },
    },
)
