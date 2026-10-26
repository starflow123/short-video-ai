"""生成质量统计：从旁路台账聚合，回答「最近生成得怎么样、哪个厂商稳」。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.keyframe import ShotKeyframe
from app.models.ledger import GenerationLedger
from app.models.media import MediaAsset
from app.models.user import User

router = APIRouter(prefix="/stats", tags=["生成统计"])


@router.get("/generations")
async def generation_stats(
    project_id: int | None = Query(default=None),
    days: int = Query(default=7, ge=1, le=90),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    conditions = [GenerationLedger.user_id == user.id, GenerationLedger.created_at >= since]
    if project_id is not None:
        conditions.append(GenerationLedger.project_id == project_id)
    base = select(GenerationLedger).where(*conditions)

    rows = (await db.execute(base)).scalars().all()
    total = len(rows)
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_provider: dict[str, dict[str, int]] = {}
    by_day: dict[str, dict[str, int]] = {}
    durations: list[int] = []
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_kind[row.kind] = by_kind.get(row.kind, 0) + 1
        provider = row.provider or "unknown"
        bucket = by_provider.setdefault(provider, {"total": 0, "succeeded": 0, "failed": 0})
        bucket["total"] += 1
        if row.status in ("succeeded", "failed"):
            bucket[row.status] += 1
        day = row.created_at.strftime("%Y-%m-%d") if row.created_at else "unknown"
        by_day.setdefault(day, {"total": 0, "succeeded": 0})["total"] += 1
        if row.status == "succeeded":
            by_day[day]["succeeded"] += 1
            if row.duration_ms:
                durations.append(row.duration_ms)
    succeeded = by_status.get("succeeded", 0)

    # 质量维度实时关联：视觉初审结论（media.meta.visual_review）与人工认可（shot_keyframes.approved_at）
    media_ids = [row.media_id for row in rows if row.media_id]
    review_by_media: dict[int, str] = {}
    approved_media: set[int] = set()
    if media_ids:
        media_rows = (
            await db.execute(
                select(MediaAsset.id, MediaAsset.meta).where(MediaAsset.id.in_(media_ids))
            )
        ).all()
        for media_id, meta in media_rows:
            verdict = ((meta or {}).get("visual_review") or {}).get("verdict")
            if verdict:
                review_by_media[int(media_id)] = str(verdict)
        approved_rows = (
            await db.execute(
                select(ShotKeyframe.media_id).where(
                    ShotKeyframe.media_id.in_(media_ids),
                    ShotKeyframe.approved_at.is_not(None),
                )
            )
        ).scalars().all()
        approved_media = {int(value) for value in approved_rows}

    review_counts: dict[str, int] = {}
    approved_count = 0
    for row in rows:
        if row.media_id and row.media_id in review_by_media:
            verdict = review_by_media[row.media_id]
            review_counts[verdict] = review_counts.get(verdict, 0) + 1
        if row.media_id and row.media_id in approved_media:
            approved_count += 1

    return {
        "days": days,
        "project_id": project_id,
        "total": total,
        "succeeded": succeeded,
        "success_rate": round(succeeded / total, 3) if total else None,
        "avg_duration_sec": round(sum(durations) / len(durations) / 1000, 1) if durations else None,
        # 平均耗时只统计成功任务：失败任务的耗时取决于重试策略，混在一起会失真
        "avg_duration_basis": "succeeded",
        "by_status": by_status,
        "by_kind": by_kind,
        "by_provider": by_provider,
        "by_day": dict(sorted(by_day.items())),
        "review_verdicts": review_counts,
        "human_approved": approved_count,
    }
