"""金镜头快照导出器：把指定项目分镜的提示词编译输入冻结成 JSON。

在容器内运行（需要数据库）：

    docker compose exec backend python /app/scripts/export_eval_snapshots.py \
        --project 49 --kinds image,video

输出落在 backend/tests/fixtures/prompt_snapshots/p{project}_s{shot}_{kind}.json。
回归测试（tests/test_prompt_regression.py）用冻结输入重编译并跑编译断言；
expectations.json 里的 must_contain 手工维护，重导不会覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.episode import Episode
from app.models.project import Project
from app.models.shot import Shot
from app.services import generation

FIXTURE_DIR = Path("/app/tests/fixtures/prompt_snapshots")


def forbid_dropped(inputs: dict) -> list[str]:
    """哪些块当时有输入、编译后必须还活着（被裁=断言失败）。

    只列编译器 protected 集合里真正承诺保全的硬约束；directives / negative /
    continuity 在极端兜底层允许整块丢弃（前端可见告警），不进把关。
    """
    references = inputs.get("references") or []
    subjects = inputs.get("subjects") or []
    guarded: list[str] = []
    if references:
        guarded.append("legend")
    if subjects:
        guarded.append("subjects")
    if subjects and any(str(ref.get("kind")) == "continuity" for ref in references):
        guarded.append("count_check")
    if inputs.get("timeline"):
        guarded.append("timeline")
    if inputs.get("start_state") or inputs.get("end_state"):
        guarded.append("state")
    return guarded


async def main(project_id: int, kinds: list[str], only_shots: list[int] | None) -> int:
    async with AsyncSessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            print(f"project {project_id} not found")
            return 1
        episodes = (
            await session.execute(
                select(Episode).where(Episode.project_id == project_id).order_by(Episode.order_index)
            )
        ).scalars().all()
        written = 0
        for episode in episodes:
            shots = (
                await session.execute(
                    select(Shot).where(Shot.episode_id == episode.id).order_by(Shot.order_index)
                )
            ).scalars().all()
            for shot in shots:
                if only_shots and shot.id not in only_shots:
                    continue
                for kind in kinds:
                    inputs = await generation.build_shot_compile_inputs(
                        session, user_id=project.user_id, shot=shot, kind=kind
                    )
                    snapshot = {
                        "project_id": project_id,
                        "project_title": project.title,
                        "shot_id": shot.id,
                        "shot_order": shot.order_index,
                        "kind": kind,
                        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "expect": {"forbid_dropped": forbid_dropped(inputs), "max_chars": inputs["max_chars"]},
                        "inputs": inputs,
                    }
                    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
                    path = FIXTURE_DIR / f"p{project_id}_s{shot.id}_{kind}.json"
                    path.write_text(
                        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    print(f"wrote {path.name}")
                    written += 1
        print(f"total {written} snapshots")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=int, required=True)
    parser.add_argument("--kinds", default="image,video")
    parser.add_argument("--shots", default="", help="逗号分隔的 shot id，缺省导全部分镜")
    args = parser.parse_args()
    shots = [int(x) for x in args.shots.split(",") if x.strip()] or None
    raise SystemExit(asyncio.run(main(args.project, [k.strip() for k in args.kinds.split(",")], shots)))
