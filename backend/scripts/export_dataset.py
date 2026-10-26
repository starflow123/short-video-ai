"""数据集导出：把台账里已认可的生成记录导成 JSONL（评测/微调原料）。

在容器内运行：

    docker compose exec backend python /app/scripts/export_dataset.py \
        --project 49 --approved-only --out /tmp/dataset.jsonl

每个样本：prompt_used（完整指令）+ prompt_clean + 参考图身份 + 视觉初审结论
+ 是否人工认可。质量差的不删，带 verdict 一起导，训练/评测时自行过滤。
默认只导图片 / 视频：诊断、导出、剪辑任务没有 prompt，混进来只是噪声。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.keyframe import ShotKeyframe
from app.models.ledger import GenerationLedger
from app.models.media import MediaAsset

MEDIA_KINDS = ("image", "video")


async def main(
    project_id: int | None,
    approved_only: bool,
    out: str,
    include_non_media: bool = False,
) -> int:
    conditions = [GenerationLedger.status == "succeeded"]
    if not include_non_media:
        conditions.append(GenerationLedger.kind.in_(MEDIA_KINDS))
    if project_id is not None:
        conditions.append(GenerationLedger.project_id == project_id)
    written = 0
    async with AsyncSessionLocal() as session:
        ledger_rows = (
            await session.execute(
                select(GenerationLedger).where(*conditions).order_by(GenerationLedger.id)
            )
        ).scalars().all()
        media_ids = [row.media_id for row in ledger_rows if row.media_id]
        media_by_id: dict[int, MediaAsset] = {}
        if media_ids:
            media_rows = (
                await session.execute(select(MediaAsset).where(MediaAsset.id.in_(media_ids)))
            ).scalars().all()
            media_by_id = {item.id: item for item in media_rows}
        approved_media: set[int] = set()
        if approved_only and media_ids:
            approved_rows = (
                await session.execute(
                    select(ShotKeyframe.media_id).where(
                        ShotKeyframe.media_id.in_(media_ids),
                        ShotKeyframe.approved_at.is_not(None),
                    )
                )
            ).scalars().all()
            approved_media = {int(value) for value in approved_rows if value}

        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for row in ledger_rows:
                media = media_by_id.get(row.media_id) if row.media_id else None
                if approved_only and row.media_id not in approved_media:
                    continue
                meta = media.meta or {} if media else {}
                sample = {
                    "task_id": row.task_id,
                    "kind": row.kind,
                    "provider": row.provider,
                    "model": row.model,
                    "project_id": row.project_id,
                    "shot_id": row.shot_id,
                    "media_id": row.media_id,
                    "prompt_version": row.prompt_version,
                    "prompt_used": meta.get("prompt_used"),
                    "prompt_clean": meta.get("prompt_clean"),
                    "reference_roles": meta.get("reference_roles"),
                    "reference_used": meta.get("reference_used"),
                    "review_verdict": (meta.get("visual_review") or {}).get("verdict"),
                    "human_approved": row.media_id in approved_media if row.media_id else False,
                    "duration_ms": row.duration_ms,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
                written += 1
    print(f"wrote {written} samples to {out}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=int, default=None)
    parser.add_argument("--approved-only", action="store_true", help="只导出人工认可的关键帧")
    parser.add_argument("--out", default="/tmp/dataset.jsonl")
    parser.add_argument(
        "--include-non-media",
        action="store_true",
        help="连诊断/导出等没有提示词的任务也导出（默认只导图片/视频）",
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            main(args.project, args.approved_only, args.out, args.include_non_media)
        )
    )
