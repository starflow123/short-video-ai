"""金镜头回归 CLI：容器内跑，结果落 eval_runs 并打印摘要。

    # 审现有主关键帧（不耗生成额度）
    docker compose exec backend python /app/scripts/run_golden_eval.py --project 49

    # 按当前编译器重新出图再审（耗额度，产物隔离不挂镜头）
    docker compose exec backend python /app/scripts/run_golden_eval.py --project 49 --mode generate
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import AsyncSessionLocal
from app.services.eval_runs import run_golden_regression


async def main(project_id: int, user_id: int, mode: str) -> int:
    async with AsyncSessionLocal() as session:
        outcome = await run_golden_regression(
            session, user_id=user_id, project_id=project_id, mode=mode
        )
        await session.commit()
    print(outcome)
    return 0 if outcome.get("status") == "completed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=int, required=True)
    parser.add_argument("--user", type=int, default=1, help="项目归属用户 id")
    parser.add_argument(
        "--mode", choices=["rejudge", "generate"], default="rejudge", help="回归口径"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.project, args.user, args.mode)))
