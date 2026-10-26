"""教训库真实场景验收：沉淀→待认可→认可→注入/拦截→过期→复活→清理。

在容器内运行：

    docker compose exec backend python /app/scripts/scenario_knowledge.py \
        --user-id 8 --project 49

所有步骤都用真实数据库和真实 HTTP 接口；结束时会删掉本次测试条目（含失败时）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.session import AsyncSessionLocal
from app.models.knowledge import KnowledgeNote
from app.models.project import Project
from app.services.knowledge import stale_sweep

TITLE = "[场景测试] 日系治愈构图留白与暖光逆光"
TAG = "日系治愈"
SUSPENSE_STYLE = "写实摄影，悬疑氛围"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


async def _project_style(session, project_id: int) -> list[str]:
    project = await session.get(Project, project_id)
    if project is None:
        raise AssertionError(f"项目 {project_id} 不存在")
    return [item for item in (project.visual_style, project.style) if item]


async def _lookup(client: httpx.AsyncClient, token: str, **params) -> list[dict]:
    response = await client.get(
        "/api/knowledge/lookup",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()


async def main(user_id: int, project_id: int) -> int:
    async with AsyncSessionLocal() as session:
        styles = await _project_style(session, project_id)
        style_text = " ".join(styles)
        check(TAG in style_text, f"项目 {project_id} 画风包含标签「{TAG}」")

    token = create_access_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    note_id: int | None = None
    try:
        async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=30.0) as client:
            # 1) 沉淀一条新教训
            created = await client.post(
                "/api/knowledge",
                headers=headers,
                json={
                    "scope": "global",
                    "category": "heuristic",
                    "title": TITLE,
                    "content": "人物坐在画面下三分之一，左侧留出公路与海平线，夕照从右后侧逆光，发丝边缘透光。",
                    "preconditions": {"media_type": "image"},
                    "visual_style": TAG,
                },
            )
            created.raise_for_status()
            note = created.json()
            note_id = int(note["id"])
            check(note["approved"] is False, "新沉淀条目默认未认可")

            listing = await client.get("/api/knowledge", headers=headers)
            listing.raise_for_status()
            check(
                all(item["id"] != note_id for item in listing.json()),
                "未认可条目不出现在默认列表",
            )

            # 2) 未认可时，修复 agent 的检索入口拿不到
            before = await _lookup(
                client,
                token,
                project_id=project_id,
                visual_style=style_text,
                media_type="image",
            )
            check(all(item["id"] != note_id for item in before), "未认可条目不会被检索注入")

            # 3) 人工认可
            approved = await client.post(f"/api/knowledge/{note_id}/approve", headers=headers)
            approved.raise_for_status()
            check(approved.json()["approved"] is True, "人工认可后状态变为已认可")

            # 4) 同画风项目命中
            after = await _lookup(
                client,
                token,
                project_id=project_id,
                visual_style=style_text,
                media_type="image",
            )
            check(any(item["id"] == note_id for item in after), "同画风项目检索命中新条目")

            # 5) 不同画风项目被资格门挡下
            other = await _lookup(
                client,
                token,
                project_id=project_id,
                visual_style=SUSPENSE_STYLE,
                media_type="image",
            )
            check(all(item["id"] != note_id for item in other), "画风不符的项目检索不到该条目")

            # 6) 修复 agent 真实检索入口 + 使用打点
            from app.services.agents.repair import _lessons_block, _lookup_lessons

            async with AsyncSessionLocal() as session:
                lessons = await _lookup_lessons(
                    session,
                    user_id=user_id,
                    project_id=project_id,
                    media_type="image",
                    visual_style=style_text,
                )
                await session.commit()
                check(any(item.id == note_id for item in lessons), "修复 agent 检索到该条目")
                check(TITLE in _lessons_block(lessons), "教训已进入修复 agent 的 system prompt 块")

            used = await client.get(
                "/api/knowledge", headers=headers, params={"include_unapproved": True}
            )
            used.raise_for_status()
            row = next(item for item in used.json() if item["id"] == note_id)
            check(row["last_used_at"] is not None, "注入后回写了 last_used_at")

            # 7) 人为回拨创建时间并清空确认/使用时间，跑过期巡检
            async with AsyncSessionLocal() as session:
                stored = (
                    await session.execute(select(KnowledgeNote).where(KnowledgeNote.id == note_id))
                ).scalar_one()
                stored.created_at = datetime.now(timezone.utc) - timedelta(days=100)
                stored.last_confirmed_at = None
                stored.last_used_at = None
                await session.commit()
                swept = await stale_sweep(session)
                await session.commit()
                check(swept >= 1, "过期巡检把长期未确认的条目降为 stale")

            stale_lookup = await _lookup(
                client,
                token,
                project_id=project_id,
                visual_style=style_text,
                media_type="image",
            )
            check(all(item["id"] != note_id for item in stale_lookup), "stale 条目不再注入")

            # 8) 人工确认复活
            revived = await client.post(f"/api/knowledge/{note_id}/confirm", headers=headers)
            revived.raise_for_status()
            check(revived.json()["status"] == "active", "人工确认后条目复活")
            revived_lookup = await _lookup(
                client,
                token,
                project_id=project_id,
                visual_style=style_text,
                media_type="image",
            )
            check(any(item["id"] == note_id for item in revived_lookup), "复活后重新可注入")

            # 9) 清理
            deleted = await client.delete(f"/api/knowledge/{note_id}", headers=headers)
            check(deleted.status_code == 204, "测试条目已删除")
            note_id = None
            final = await _lookup(
                client,
                token,
                project_id=project_id,
                visual_style=style_text,
                media_type="image",
            )
            check(all(item["title"] != TITLE for item in final), "删除后不再出现在检索结果")
    finally:
        if note_id is not None:
            async with AsyncSessionLocal() as session:
                leftover = await session.get(KnowledgeNote, note_id)
                if leftover is not None:
                    await session.delete(leftover)
                    await session.commit()
                    print(f"CLEANUP 已清理残留测试条目 {note_id}")
    print("SCENARIO OK")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, default=8)
    parser.add_argument("--project", type=int, default=49)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.user_id, args.project)))
