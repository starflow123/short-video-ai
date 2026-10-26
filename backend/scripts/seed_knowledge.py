"""种子生产教训：只收用户实测验证过的结论，approved_at 直接置上。

    docker compose exec backend python /app/scripts/seed_knowledge.py --user-id 8
不传 --user-id 时默认给「拥有项目最多的用户」补种；重复执行安全（按 title 幂等）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.knowledge import KnowledgeNote
from app.models.project import Project
from app.models.user import User

SEEDS = [
    {
        "scope": "global",
        "category": "fact",
        "title": "Agnes 视频接口只认 size=720P（大写）",
        "content": "POST /videos 的合法字段是 size 而不是 resolution，且只有 720P 一档、必须大写；"
        "小写 720p 会返回 402 plan_upgrade_required，resolution 字段直接 400。"
        "请求其它档位时按 720P 出片并在 meta.resolution_note 里说明，禁止静默降级。",
        "preconditions": {"provider": "agnes", "media_type": "video"},
    },
    {
        "scope": "global",
        "category": "heuristic",
        "title": "同角色坐/站姿态突变时不要用图生图接力",
        "content": "同一角色在相邻镜头间坐姿↔站姿突变时，图生图会把参考帧里的人留在原位再补一个，"
        "实测连续三次画成 3 个人。姿态突变就不要把上一镜关键帧作为参考传进去（宁可少一张参考），"
        "根治方向是回读上一镜视频尾帧作锚点 + 分镜层「起幅=落幅」校验。",
        "preconditions": {"media_type": "image"},
    },
    {
        "scope": "global",
        "category": "heuristic",
        "title": "参考图图例被预算截断会分身",
        "content": "【参考图】图例块被字数预算截到十几字时（实测 17 字），模型会照参考图把角色复制成两个。"
        "图例必须保留「图N=角色/道具」编号对应 + 「张数≠人数，同一角色只画一次」；"
        "保不住完整语义时整块丢弃让前端告警，绝不截成半句。",
        "preconditions": {"media_type": "image"},
    },
    {
        "scope": "global",
        "category": "heuristic",
        "title": "动物不计入人数硬约束",
        "content": "主体里有猫/狗等动物时，人数硬约束必须把动物从人头数里拆出去单列"
        "（「X 是动物，按外观描述画成对应动物，不计入人数」），"
        "否则模型会补一个人类凑数或把动物画成人。",
        "preconditions": {"media_type": "image"},
    },
    {
        "scope": "global",
        "category": "heuristic",
        "title": "场景判定取 location 的最后一段（最具体的地名）",
        "content": "同场景接力判定用 start_state.location 的最后一个逗号段（最具体的地点），"
        "剥括号后缀。取第一段会把「海边小镇的窄巷口」和「海边小镇的巴士站」误判成同场景，"
        "把海滩画面接力进巷子；取最后一段则 面包店门口↔面包店橱窗 仍正确同场景。",
        "preconditions": {"media_type": "image"},
    },
]


async def main(user_id: int | None) -> int:
    async with AsyncSessionLocal() as session:
        user = None
        if user_id is not None:
            user = await session.get(User, user_id)
        else:
            # 不能拿「第一个注册用户」当默认：那是冒烟账号，真实账号一条都查不到
            owner = (
                await session.execute(
                    select(Project.user_id, func.count(Project.id).label("n"))
                    .group_by(Project.user_id)
                    .order_by(func.count(Project.id).desc(), Project.user_id)
                    .limit(1)
                )
            ).first()
            if owner is not None:
                user = await session.get(User, int(owner.user_id))
        if user is None:
            print(f"user {user_id} not found" if user_id else "no project owner found")
            return 1
        print(f"seeding for user id={user.id} username={user.username}")
        now = datetime.now(timezone.utc)
        created = 0
        for seed in SEEDS:
            exists = (
                await session.execute(
                    select(KnowledgeNote.id).where(
                        KnowledgeNote.user_id == user.id, KnowledgeNote.title == seed["title"]
                    )
                )
            ).scalar_one_or_none()
            if exists:
                continue
            session.add(
                KnowledgeNote(
                    user_id=user.id,
                    project_id=None,
                    scope=seed["scope"],
                    category=seed["category"],
                    title=seed["title"],
                    content=seed["content"],
                    preconditions=seed["preconditions"],
                    visual_style=None,
                    status="active",
                    approved_at=now,
                    last_confirmed_at=now,
                )
            )
            created += 1
        await session.commit()
        print(f"seeded {created} notes (skipped {len(SEEDS) - created} existing)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.user_id)))
