"""手动集成检查：分镜结构化字段落库 → API 序列化 → 清理。

在容器内运行：
    python tests/manual_shot_context_check.py
"""
from __future__ import annotations

import asyncio
import uuid

from app.api.serialize import serialize_shots
from app.db.session import AsyncSessionLocal
from app.models.asset import ProductionAsset
from app.models.episode import Episode
from app.models.project import Project
from app.models.user import User
from app.services import assets as asset_service
from app.services.generation import _apply_breakdown


async def main() -> None:
    async with AsyncSessionLocal() as session:
        user = User(
            username=f"ctx_check_{uuid.uuid4().hex[:8]}",
            hashed_password="not-a-real-hash",
        )
        session.add(user)
        await session.flush()
        project = Project(user_id=user.id, title="context check", aspect_ratio="9:16")
        session.add(project)
        await session.flush()
        episode = Episode(project_id=project.id, order_index=1, title="context check")
        session.add(episode)
        await session.flush()

        characters = await asset_service.ensure_character_assets(
            session,
            user_id=user.id,
            project_id=project.id,
            profiles=[
                {
                    "name": "杨间",
                    "age": "25 岁上下",
                    "role": "刑警",
                    "appearance": "短碎发，眉眼锐利",
                    "costume": "黑色连帽卫衣",
                    "traits": "冷静",
                    "tags": ["黑发"],
                }
            ],
        )
        assert characters[0]["asset_id"]
        # 同名重复建档应复用同一个资产
        again = await asset_service.ensure_character_assets(
            session,
            user_id=user.id,
            project_id=project.id,
            profiles=[{"name": "杨间", "appearance": "短碎发，眉眼锐利", "costume": "黑色连帽卫衣"}],
        )
        assert again[0]["asset_id"] == characters[0]["asset_id"]
        print("PASS: 角色资产建/复用正常")

        payload = {
            "synopsis": "临时校验分镜上下文",
            "shots": [
                {
                    "order_index": 1,
                    "title": "雨巷撑伞",
                    "description": "杨间在雨巷撑开黑伞",
                    "duration_seconds": 5,
                    "subjects": [
                        {
                            "name": "杨间",
                            "position": "画面左侧，面向右",
                            "action": "撑开黑伞",
                            "expression": "警惕",
                            "gaze": "看向巷口",
                            "holds": "黑色鬼伞",
                            "is_primary": True,
                        }
                    ],
                    "props": [{"name": "黑色鬼伞", "owner": "杨间", "state": "撑开"}],
                    "action_timeline": [
                        {"start": 0, "end": 2, "action": "抬手撑伞", "expression": "警觉"},
                        {"start": 2, "end": 5, "action": "转身", "gaze": "望向巷口"},
                    ],
                    "start_state": {"location": "雨巷", "time_of_day": "夜"},
                    "end_state": {"positions": "持伞转身"},
                    "image_prompt": "雨夜巷口，杨间撑开黑伞",
                    "video_prompt": "0-2s 撑伞；2-5s 转身",
                }
            ],
        }
        shots = await _apply_breakdown(session, episode, payload)
        binding = await asset_service.bind_shot_assets(
            session, user_id=user.id, project_id=project.id, shots=shots
        )
        assert shots[0].subjects[0]["asset_id"] == characters[0]["asset_id"]
        assert shots[0].subjects[0]["appearance"] == "短碎发，眉眼锐利"
        assert shots[0].props[0]["asset_id"]
        assert binding["new_props"] == 1
        print("PASS: 镜头主体/道具已绑定资产")
        await session.commit()

        items = await serialize_shots(session, shots)
        item = items[0]
        assert item.subjects[0].name == "杨间"
        assert item.subjects[0].holds == "黑色鬼伞"
        assert item.props[0].owner == "杨间"
        assert len(item.action_timeline) == 2
        assert item.action_timeline[1].end == 5
        assert item.start_state["location"] == "雨巷"
        assert item.end_state["positions"] == "持伞转身"
        print("PASS: 落库与序列化正常")

        await session.delete(user)
        await session.commit()
        print("PASS: 临时数据已清理")


if __name__ == "__main__":
    asyncio.run(main())
