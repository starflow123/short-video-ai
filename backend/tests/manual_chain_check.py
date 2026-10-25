"""串行链推进自检：链尾投递、状态收敛（images_ready / videos_ready / completed）。

跑法（容器内，用真实库但把 Celery 投递打桩掉，不会真的出图）：
    docker compose -f deploy/docker-compose.yml exec -T backend sh -c \\
      'env PYTHONPATH=/app python tests/manual_chain_check.py'
"""
from __future__ import annotations

import asyncio
import uuid

from app.db.session import AsyncSessionLocal
from app.models.episode import Episode
from app.models.pipeline import PipelineRun
from app.models.project import Project
from app.models.shot import Shot
from app.models.task import GenerationTask
from app.models.user import User
from app.services import dispatch
from app.services.generation import _advance_chain

ENQUEUED: list[int] = []


async def _fake_enqueue(session, task):
    ENQUEUED.append(task.id)
    task.status = "running"
    await session.commit()


async def main() -> None:
    dispatch.enqueue_task = _fake_enqueue

    async with AsyncSessionLocal() as session:
        user = User(
            username=f"chain_check_{uuid.uuid4().hex[:8]}",
            hashed_password="not-a-real-hash",
        )
        session.add(user)
        await session.flush()

        project = Project(user_id=user.id, title="链式自检", aspect_ratio="9:16")
        session.add(project)
        await session.flush()

        episode = Episode(project_id=project.id, order_index=1, title="第1集")
        session.add(episode)
        await session.flush()

        shots = []
        for index in (1, 2):
            shot = Shot(
                episode_id=episode.id,
                order_index=index,
                title=f"镜头{index}",
                description="测试",
                status="pending",
                prompt_confirmed=True,
            )
            session.add(shot)
            shots.append(shot)
        await session.flush()

        run = PipelineRun(
            user_id=user.id,
            project_id=project.id,
            episode_id=episode.id,
            status="generating",
            stage="generate",
            params={},
        )
        session.add(run)
        await session.commit()

        def make_task(kind: str, shot_id: int | None, flow: str, next_id: int | None) -> GenerationTask:
            task = GenerationTask(
                user_id=user.id,
                kind=kind,
                project_id=project.id,
                episode_id=episode.id,
                shot_id=shot_id,
                status="pending",
                params={
                    "flow": flow,
                    "run_id": run.id,
                    **({"chain_next": next_id} if next_id else {}),
                },
            )
            session.add(task)
            return task

        # 图链：t1 -> t2 -> 链尾（必须先 flush 拿到 t2.id，才能把它写成 t1 的下一环）
        t2 = make_task("image", shots[1].id, "pipeline_images", None)
        await session.flush()
        t1 = make_task("image", shots[0].id, "pipeline_images", t2.id)
        await session.commit()

        ENQUEUED.clear()
        await _advance_chain(session, t1.id, succeeded=True)
        assert ENQUEUED == [t2.id], f"第二个任务应被投递：{ENQUEUED}"
        await session.refresh(run)
        assert run.status == "generating", run.status

        await _advance_chain(session, t2.id, succeeded=True)
        await session.refresh(run)
        assert run.status == "images_ready" and run.stage == "images_ready", run.status
        print("PASS: 生图串行链跑完 → images_ready")

        # 视频链：只有一个镜头出了视频 → videos_ready（不该假装「已完成」）
        shots[0].video_media_id = None
        shots[1].video_media_id = None
        v1 = make_task("video", shots[0].id, "pipeline_videos", None)
        shots[0].video_media_id = 999_999  # 占位，仅表示「这个镜头已有视频」
        await session.commit()

        await _advance_chain(session, v1.id, succeeded=True)
        await session.refresh(run)
        assert run.status == "videos_ready" and run.stage == "videos_ready", run.status
        print("PASS: 视频没出齐 → videos_ready，不提前判定完成")

        # 全项目镜头都有视频 → completed
        shots[1].video_media_id = 999_998
        v2 = make_task("video", shots[1].id, "pipeline_videos", None)
        await session.commit()
        await _advance_chain(session, v2.id, succeeded=True)
        await session.refresh(run)
        assert run.status == "completed" and run.stage == "done", run.status
        print("PASS: 镜头视频齐了 → completed")

        # 导出失败必须让流水线失败，不能停在「生成中」
        e1 = make_task("export", None, "pipeline_export", None)
        await session.commit()
        await _advance_chain(session, e1.id, succeeded=False, error="未检测到 ffmpeg")
        await session.refresh(run)
        assert run.status == "failed" and "ffmpeg" in (run.error or ""), (run.status, run.error)
        print("PASS: 导出失败 → 流水线标记失败并带上原因")

        await session.delete(user)
        await session.commit()
        print("PASS: 临时数据已清理")


asyncio.run(main())
