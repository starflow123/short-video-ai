"""RSI 提案 CLI：为白名单里的一个全局编译模板键提交候选 patch。

验证不过直接拒绝；过了也只是进待认可队列，人工认可才生效。

    docker compose exec backend python /app/scripts/rsi_propose.py \
        --key video_continuity_compact --content-file /tmp/patch.txt \
        --rationale "切点审查发现 X" --user 1
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import AsyncSessionLocal
from app.services.compiler_templates import propose


async def main(key: str, content: str, rationale: str | None, user_id: int) -> int:
    async with AsyncSessionLocal() as session:
        try:
            row = await propose(
                session, user_id=user_id, key=key, content=content, rationale=rationale
            )
        except ValueError as exc:
            print(f"REJECTED: {exc}")
            return 1
        await session.commit()
        print(f"PROPOSED: id={row.id} key={row.key} version={row.version}")
        print("等人工认可后才生效：POST /api/compiler-templates/{id}/approve")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--content", default="", help="直接给内容；或用 --content-file")
    parser.add_argument("--content-file", default="")
    parser.add_argument("--rationale", default=None)
    parser.add_argument("--user", type=int, default=1)
    args = parser.parse_args()
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    else:
        content = args.content
    raise SystemExit(asyncio.run(main(args.key, content, args.rationale, args.user)))
